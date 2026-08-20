"""
Read-only REST surface for the Klikk books knowledge base (Postgres schema ``kb``) —
consumed by the klikk-financials MCP server (kb_* tools) and any other authenticated
agent (console JWT, service token, future GPT Actions).

GET /api/kb/documents/                 list the doctrine documents (slug, title, size)
GET /api/kb/documents/<slug>/          one document, full markdown body
GET /api/kb/search/?q=&limit=          full-text search over the documents (websearch syntax)
GET /api/kb/suppliers/?name=&strength= supplier default-coding rules (partial name match)
GET /api/kb/customers/?name=           customer income-coding rules
GET /api/kb/accounts/?q=               chart-of-accounts dictionary (code or name match)
GET /api/kb/tracking/?slot=&q=         tracking options (slot 1 Profit Center / 2 Room / 3 Custom)
GET /api/kb/events/?q=&on=             event/gig register: date windows that turn personal-looking spend into event costs

The ``kb`` schema is a local register seeded from the observed 2016–2025 Xero mirror
(doctrine master: klikk-books-kb — see kb.documents itself). Rows with
``reviewed_by_mc = false`` are observed patterns, not MC-confirmed rules.
Everything here is read-only; nothing ever calls or writes to Xero.
"""
from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

# rule_strength semantics, shared by suppliers/ and customers/ (kept in the API answer
# so machine callers don't need the doctrine docs to interpret a rule).
STRENGTH_LEGEND = {
    'hard': '>=80% historical consistency - safe to auto-code',
    'soft': '50-79% - default, verify before applying',
    'info': '<50% - line-level judgement required (e.g. hardware retailers, municipal splits)',
}

MAX_LIMIT = 50


def _rows(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _limit(request, default=20):
    try:
        n = int(request.query_params.get('limit', default))
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_LIMIT))


@api_view(['GET'])
def list_documents(request):
    return Response(_rows(
        "SELECT slug, title, length(body) AS chars, updated_at "
        "FROM kb.documents ORDER BY slug"))


@api_view(['GET'])
def read_document(request, slug):
    rows = _rows("SELECT slug, title, body, updated_at FROM kb.documents WHERE slug = %s", [slug])
    if not rows:
        return Response({'detail': f'No document {slug!r}. List them at /api/kb/documents/.'},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(rows[0])


@api_view(['GET'])
def search(request):
    q = (request.query_params.get('q') or '').strip()
    if not q:
        return Response({'detail': 'Pass ?q=<websearch query>, e.g. ?q=personal groceries loan.'},
                        status=status.HTTP_400_BAD_REQUEST)
    return Response(_rows(
        "SELECT slug, title, "
        "       ts_headline('english', body, websearch_to_tsquery('english', %s), "
        "                   'MaxFragments=3, MaxWords=40, MinWords=10') AS snippets, "
        "       ts_rank(fts, websearch_to_tsquery('english', %s)) AS rank "
        "FROM kb.documents "
        "WHERE fts @@ websearch_to_tsquery('english', %s) "
        "ORDER BY rank DESC LIMIT %s", [q, q, q, _limit(request, 5)]))


@api_view(['GET'])
def suppliers(request):
    name = (request.query_params.get('name') or '').strip()
    strength = (request.query_params.get('strength') or '').strip()
    conds, params = ['true'], []
    if name:
        conds.append('contact_pattern ILIKE %s')
        params.append(f'%{name}%')
    if strength:
        conds.append('rule_strength = %s')
        params.append(strength)
    params.append(_limit(request))
    rows = _rows(
        "SELECT contact_pattern, expected_account, account_name, dominant_pct, "
        "       expected_tax, expected_tracking1, rule_strength, lines, total_spend, "
        "       reviewed_by_mc, notes "
        f"FROM kb.supplier_rules WHERE {' AND '.join(conds)} "
        "ORDER BY total_spend DESC NULLS LAST LIMIT %s", params)
    return Response({'legend': STRENGTH_LEGEND, 'rules': rows})


@api_view(['GET'])
def customers(request):
    name = (request.query_params.get('name') or '').strip()
    conds, params = ['true'], []
    if name:
        conds.append('contact_pattern ILIKE %s')
        params.append(f'%{name}%')
    params.append(_limit(request))
    rows = _rows(
        "SELECT contact_pattern, expected_account, account_name, dominant_pct, "
        "       expected_tax, expected_tracking1, rule_strength, lines, total_income, "
        "       reviewed_by_mc, notes "
        f"FROM kb.customer_rules WHERE {' AND '.join(conds)} "
        "ORDER BY total_income DESC NULLS LAST LIMIT %s", params)
    return Response({'legend': STRENGTH_LEGEND, 'rules': rows})


@api_view(['GET'])
def accounts(request):
    q = (request.query_params.get('q') or '').strip()
    conds, params = ['true'], []
    if q:
        conds.append('(code ILIKE %s OR name ILIKE %s)')
        params += [f'%{q}%', f'%{q}%']
    params.append(_limit(request, 25))
    return Response(_rows(
        "SELECT code, name, type, n_lines, net_amt, last_used, "
        "       meaning, deductibility, when_to_use, when_not_to_use, reviewed_by_mc "
        f"FROM kb.account_dictionary WHERE {' AND '.join(conds)} "
        "ORDER BY n_lines DESC NULLS LAST LIMIT %s", params))


@api_view(['GET'])
def tracking(request):
    slot = (request.query_params.get('slot') or '').strip()
    q = (request.query_params.get('q') or '').strip()
    conds, params = ['true'], []
    if slot:
        if not slot.isdigit():
            return Response({'detail': 'slot must be 1, 2 or 3'}, status=status.HTTP_400_BAD_REQUEST)
        conds.append('category_slot = %s')
        params.append(int(slot))
    if q:
        conds.append('option ILIKE %s')
        params.append(f'%{q}%')
    return Response(_rows(
        "SELECT category_slot, category, option, option_id, meaning, applies_to, reviewed_by_mc "
        f"FROM kb.tracking_dictionary WHERE {' AND '.join(conds)} "
        "ORDER BY category_slot, category, option", params))


@api_view(['GET'])
def events(request):
    """The event register (kb.events). ?on=YYYY-MM-DD returns events whose window
    covers that date (the allocation-agent event screen); ?q= filters by name."""
    on = (request.query_params.get('on') or '').strip()
    q = (request.query_params.get('q') or '').strip()
    conds, params = ['true'], []
    if on:
        conds.append('(window_start IS NOT NULL AND %s::date BETWEEN window_start AND window_end)')
        params.append(on)
    if q:
        conds.append('event_name ILIKE %s')
        params.append(f'%{q}%')
    return Response(_rows(
        'SELECT event_name, tracking_option, invoiced_by, customer, window_start, '
        '       window_end, venue, income, costs, notes, reviewed_by_mc '
        f"FROM kb.events WHERE {' AND '.join(conds)} "
        'ORDER BY window_start NULLS LAST', params))
