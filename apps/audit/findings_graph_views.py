"""
Graph traversal over the audit-findings link graph.

GET /audit/findings/graph/   [auth]   nodes + edges around a finding or an entity

WHY A POSTGRES VIEW AND A RECURSIVE CTE, AND NOT A GRAPH STORE
--------------------------------------------------------------
The "graph" here is not a separate dataset — it is a *projection* of rows this
database already owns: ``audit_auditfindinglink`` (finding -> slip / document /
bank txn / journal / invoice / asana), ``audit_auditfindingattachment``
(finding -> file) and ``audit_auditfinding.check_code`` (finding -> registry
check). ``audit.v_finding_graph`` unions those three into one edge shape; this
module walks it with ``WITH RECURSIVE``.

Standing up Neo4j/AGE alongside would buy nothing and cost a great deal: a
second store to keep in sync, a second backup, a second thing to be stale, and
a whole new class of "the graph disagrees with the register" bug — while the
traversal we actually need is depth<=2 over a few thousand edges, which
Postgres does in single-digit milliseconds off the ``(kind, ref)`` index. The
edges ARE the rows; a copy of them would only be able to be wrong. If the graph
ever grows a genuine multi-hop analytical workload (shortest path over 10^6
edges, community detection), revisit — but revisit with a measurement, not a
preference.

TRAVERSAL IS BIDIRECTIONAL — THIS IS THE POINT
----------------------------------------------
The view stores edges *directed*: ``finding -> entity``. The headline use case
is the REVERSE one — ``?node_type=slip&node_id=<sha256>`` means "which findings
cite this slip", which walks a finding->slip edge backwards. So the walk runs
over a normalised edge set: the view UNION ALL its own transpose, with the
canonical (from/to) orientation carried alongside the walk orientation so the
response still reports edges the way they are stored. A walk that only followed
``from -> to`` would answer nothing anybody asks.

The edge set is a ``NOT MATERIALIZED`` CTE deliberately: materialising it would
build the whole transposed graph before filtering, and the seed lookup would
lose its index. Inlined, ``a_type='slip' AND a_id=<sha>`` pushes down into the
transpose branch as ``kind='slip' AND ref=<sha>`` and hits the
``(kind, ref)`` index that ``AuditFindingLink.Meta.indexes`` exists for.

Cycles are guarded with a visited-key path array, not a depth counter alone: a
depth-2 walk from a slip goes slip -> finding -> {other entities} and must not
bounce back to the slip it started from.

CAPS
----
``depth`` is CLAMPED, never rejected — ``depth=99`` is a caller being optimistic,
not a caller being wrong, and a 400 there just makes the console handle an error
it can do nothing about. Garbage or 0 -> 1, anything above -> 2.

The 500-edge cap is applied as a SQL ``LIMIT`` (cap + 1, so one extra row tells
us the cap bit) rather than by slicing a Python list, because the whole reason
this is a database traversal is to not drag the graph into the web worker.

Label resolution is BATCHED — one query per node TYPE, never one per node. A
graph with 200 slip nodes fires one slip query. A ref that resolves to nothing
still appears as a node labelled with its raw id: dangling refs are NORMAL here
(a slip can be purged while the finding citing it must survive), so this
endpoint degrades, it does not 500.
"""
import datetime as dt
from decimal import Decimal

from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.slip_view import slip_url
from apps.xero.xero_data.document_views import xero_document_url

from .findings_services import attachment_url, current_fy, resolve_default_fy
from .models import LINK_KINDS, AuditFinding, AuditFindingAttachment

# Every value that can appear as ``from_type`` or ``to_type`` in audit.v_finding_graph.
# ``finding`` is the only from_type; the rest are edge targets.
NODE_TYPES: tuple[str, ...] = ('finding', *LINK_KINDS, 'attachment', 'check')

MAX_DEPTH = 2
EDGE_CAP = 500

FY_MIN, FY_MAX = 2015, 2100

# The bidirectional edge set: the view, UNION ALL its transpose.
#
# a_type/a_id -> b_type/b_id is the WALK orientation (direction-agnostic);
# f_*/t_* carry the CANONICAL stored orientation so the response reports
# "finding -> slip" even when the walk arrived slip-first.
#
# NOT MATERIALIZED is load-bearing: it lets the seed predicate push down into
# the transpose branch as kind=/ref= and use the (kind, ref) index. See the
# module docstring.
_EDGE_SET = """
    e AS NOT MATERIALIZED (
        SELECT g.from_type AS a_type, g.from_id AS a_id,
               g.to_type   AS b_type, g.to_id   AS b_id,
               g.from_type AS f_type, g.from_id AS f_id,
               g.to_type   AS t_type, g.to_id   AS t_id,
               g.edge, g.label, g.fy
          FROM audit.v_finding_graph g
         WHERE (%(fy)s::int IS NULL OR g.fy = %(fy)s::int)
        UNION ALL
        SELECT g.to_type, g.to_id,
               g.from_type, g.from_id,
               g.from_type, g.from_id,
               g.to_type, g.to_id,
               g.edge, g.label, g.fy
          FROM audit.v_finding_graph g
         WHERE (%(fy)s::int IS NULL OR g.fy = %(fy)s::int)
    )
"""

# Walk out from one seed node in both directions, to at most %(depth)s hops,
# never revisiting a node already on the path.
WALK_SQL = f"""
WITH RECURSIVE
{_EDGE_SET},
    walk AS (
        SELECT e.f_type, e.f_id, e.edge, e.t_type, e.t_id, e.label,
               e.b_type AS nx_type, e.b_id AS nx_id,
               1 AS depth,
               ARRAY[%(node_type)s::text || ':' || %(node_id)s::text,
                     e.b_type || ':' || e.b_id] AS path
          FROM e
         WHERE e.a_type = %(node_type)s::text AND e.a_id = %(node_id)s::text
        UNION ALL
        SELECT e.f_type, e.f_id, e.edge, e.t_type, e.t_id, e.label,
               e.b_type, e.b_id,
               w.depth + 1,
               w.path || (e.b_type || ':' || e.b_id)
          FROM walk w
          JOIN e ON e.a_type = w.nx_type AND e.a_id = w.nx_id
         WHERE w.depth < %(depth)s::int
           AND NOT (e.b_type || ':' || e.b_id) = ANY(w.path)
    )
SELECT DISTINCT f_type, f_id, edge, t_type, t_id, coalesce(label, '') AS label
  FROM walk
 ORDER BY f_type, f_id, edge, t_type, t_id
 LIMIT %(limit)s
"""

# No seed node: every edge belonging to a finding in the resolved FY. That is
# already "every finding plus its depth-1 edges" — the view's fy column is the
# finding's fy — so no recursion is needed at all.
ALL_EDGES_SQL = """
SELECT g.from_type, g.from_id, g.edge, g.to_type, g.to_id, coalesce(g.label, '') AS label
  FROM audit.v_finding_graph g
 WHERE (%(fy)s::int IS NULL OR g.fy = %(fy)s::int)
 ORDER BY g.from_type, g.from_id, g.edge, g.to_type, g.to_id
 LIMIT %(limit)s
"""


def _bad(detail: str) -> Response:
    return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)


def _resolve_fy(raw: str) -> tuple[int | None, str | None]:
    """(fy filter, error). ``fy=all`` -> None. Blank -> ``resolve_default_fy()``.

    Same rule as the list endpoint, imported rather than restated — a graph that
    silently defaulted to a different year than the table beside it would be
    worse than no graph.
    """
    raw = (raw or '').replace('\x00', '').strip()
    if not raw:
        return resolve_default_fy(), None
    if raw.lower() == 'all':
        return None, None
    try:
        fy = int(raw)
    except ValueError:
        return None, f"fy must be a year between {FY_MIN} and {FY_MAX}, or 'all'"
    if not FY_MIN <= fy <= FY_MAX:
        return None, f'fy must be between {FY_MIN} and {FY_MAX}'
    return fy, None


def _clamp_depth(raw: str) -> int:
    """1..MAX_DEPTH. Garbage, 0 and negatives -> 1; anything over -> MAX_DEPTH."""
    try:
        depth = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return min(max(depth, 1), MAX_DEPTH)


# --------------------------------------------------------------------------- #
# Batched label resolution — one query per TYPE, never one per node
# --------------------------------------------------------------------------- #
def _money(value) -> str:
    if value is None:
        return ''
    return f'{Decimal(value):,.2f}'


def _date(value) -> str:
    if isinstance(value, dt.datetime):
        value = value.date()
    return value.isoformat() if isinstance(value, dt.date) else ''


def _ints(ids):
    """The subset of ``ids`` that are plain integers, as {int: original str}."""
    out = {}
    for raw in ids:
        try:
            out[int(raw)] = raw
        except (TypeError, ValueError):
            continue
    return out


def _label_findings(ids: set[str]) -> dict[str, dict]:
    by_id = _ints(ids)
    if not by_id:
        return {}
    rows = AuditFinding.objects.filter(id__in=by_id).values_list('id', 'ref', 'title')
    return {by_id[pk]: {'label': f'{ref} — {title}'.strip(' —'), 'url': None} for pk, ref, title in rows}


def _label_slips(ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    # ocr->>'total' is free text and is not always numeric — same guard the
    # receipts register uses (apps/receipts/services.py TOTAL_SQL).
    sql = """
        SELECT sha256,
               ocr->>'supplier',
               CASE WHEN ocr->>'total' ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (ocr->>'total')::numeric END,
               slip_ts
          FROM whatsapp.klikk_slips
         WHERE sha256 = ANY(%s)
    """
    out = {}
    with connection.cursor() as cur:
        cur.execute(sql, [list(ids)])
        for sha, supplier, total, slip_ts in cur.fetchall():
            parts = [p for p in (supplier, f'R{_money(total)}' if total is not None else '') if p]
            out[sha] = {
                'label': ' '.join(parts) or _date(slip_ts) or sha,
                'url': slip_url(sha),
            }
    return out


def _label_documents(ids: set[str]) -> dict[str, dict]:
    from apps.xero.xero_data.models import XeroDocument

    by_id = _ints(ids)
    if not by_id:
        return {}
    rows = XeroDocument.objects.filter(id__in=by_id).values_list('id', 'file_name')
    return {
        by_id[pk]: {'label': file_name or str(pk), 'url': xero_document_url(pk)}
        for pk, file_name in rows
    }


def _label_bank(ids: set[str]) -> dict[str, dict]:
    from django.db.models import Q

    from apps.investec.models import InvestecBankTransaction

    by_id = _ints(ids)
    numeric = {str(i) for i in by_id}
    uuids = [r for r in ids if r not in numeric]
    if not by_id and not uuids:
        return {}

    # A ref is either the integer pk or the Investec uuid — resolve BOTH in ONE
    # query. The Q is built up rather than OR-ed against an empty ``pk__in=[]``,
    # which Django folds into an EmptyResultSet and can swallow the other side.
    predicate = Q()
    if by_id:
        predicate |= Q(pk__in=list(by_id))
    if uuids:
        predicate |= Q(uuid__in=uuids)

    rows = InvestecBankTransaction.objects.filter(predicate).values_list(
        'id', 'uuid', 'transaction_date', 'amount', 'description',
    )
    out = {}
    for pk, uuid, txn_date, amount, description in rows:
        # transaction_date, NOT posting_date: posting_date is unreliable on this
        # system (every row on account 363177001 carries the same backfill date).
        # Credits are stored NEGATIVE, so the sign is shown exactly as stored.
        label = ' '.join(p for p in (_date(txn_date), _money(amount), description or '') if p)
        entry = {'label': label or str(pk), 'url': None}  # no file behind a bank line
        if pk in by_id:
            out[by_id[pk]] = entry
        if uuid and uuid in ids:
            out[uuid] = entry
    return out


def _label_journals(ids: set[str]) -> dict[str, dict]:
    by_number = _ints(ids)
    if not by_number:
        return {}
    # A journal_number covers many lines; DISTINCT ON keeps it to one row each
    # instead of dumping the lines into a label.
    sql = """
        SELECT DISTINCT ON (journal_number) journal_number, date, description
          FROM xero_data_xerojournals
         WHERE journal_number = ANY(%s)
         ORDER BY journal_number, id
    """
    out = {}
    with connection.cursor() as cur:
        cur.execute(sql, [list(by_number)])
        for number, jdate, description in cur.fetchall():
            label = ' '.join(p for p in (f'Journal {number}', _date(jdate), description or '') if p)
            out[by_number[number]] = {'label': label, 'url': None}
    return out


def _label_invoices(ids: set[str]) -> dict[str, dict]:
    from apps.xero.xero_data.models import XeroInvoice

    if not ids:
        return {}
    rows = XeroInvoice.objects.filter(invoice_number__in=ids).values_list(
        'invoice_number', 'contact_name', 'date',
    )
    out = {}
    for number, contact, idate in rows:
        out.setdefault(number, {
            'label': ' '.join(p for p in (number, contact or '', _date(idate)) if p),
            'url': None,
        })
    return out


def _label_attachments(ids: set[str]) -> dict[str, dict]:
    by_id = _ints(ids)
    if not by_id:
        return {}
    rows = AuditFindingAttachment.objects.filter(id__in=by_id).values_list('id', 'original_name')
    return {
        by_id[pk]: {'label': name or str(pk), 'url': attachment_url(pk)}
        for pk, name in rows
    }


def _label_asana(ids: set[str]) -> dict[str, dict]:
    # Deliberately no lookup: Asana is not in this database and the graph
    # endpoint does not make outbound calls.
    return {gid: {'label': gid, 'url': f'https://app.asana.com/0/0/{gid}'} for gid in ids}


def _label_checks(ids: set[str]) -> dict[str, dict]:
    # The code IS the label (CONTRACT-2 §4). No query.
    return {code: {'label': code, 'url': None} for code in ids}


_RESOLVERS = {
    'finding': _label_findings,
    'slip': _label_slips,
    'xero_document': _label_documents,
    'bank_transaction': _label_bank,
    'journal': _label_journals,
    'invoice': _label_invoices,
    'attachment': _label_attachments,
    'asana': _label_asana,
    'check': _label_checks,
}


def _build_nodes(pairs: list[tuple[str, str]]) -> list[dict]:
    """``pairs`` of (type, id) -> node dicts, one resolver query per TYPE.

    An id that no resolver could place still comes back as a node labelled with
    its raw id — dropping it would silently hide a dangling ref, which is the
    one thing an auditor most wants to see.
    """
    by_type: dict[str, set[str]] = {}
    for node_type, node_id in pairs:
        by_type.setdefault(node_type, set()).add(node_id)

    nodes = []
    for node_type in sorted(by_type):
        ids = by_type[node_type]
        resolver = _RESOLVERS.get(node_type)
        resolved = resolver(ids) if resolver else {}
        for node_id in sorted(ids):
            entry = resolved.get(node_id) or {'label': node_id, 'url': None}
            nodes.append({
                'type': node_type,
                'id': node_id,
                'label': entry['label'] or node_id,
                'url': entry['url'],
            })
    return nodes


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def findings_graph_view(request):
    params = request.query_params

    raw_fy = params.get('fy', '')
    node_seeded = bool((params.get('node_type') or '').strip())
    if node_seeded and not str(raw_fy).strip():
        # A caller who has NAMED a node is asking "which findings cite THIS", not
        # "which findings in some default year cite this". Filtering a node-seeded
        # walk on an FY the caller never mentioned silently returns nothing the
        # moment the default FY moves on -- the headline reverse lookup would go
        # dark in July. The FY default earns its keep on the no-node branch
        # ("what's open"), which is what it was designed for. ?fy= still overrides.
        fy, fy_error = None, None
    else:
        fy, fy_error = _resolve_fy(raw_fy)
    if fy_error:
        return _bad(fy_error)

    depth = _clamp_depth(params.get('depth', ''))

    node_type = (params.get('node_type') or '').replace('\x00', '').strip()
    node_id = (params.get('node_id') or '').replace('\x00', '').strip()
    if node_type and node_type not in NODE_TYPES:
        return _bad(f"node_type must be one of: {', '.join(NODE_TYPES)}")
    if bool(node_type) != bool(node_id):
        # Half a seed is a caller bug, not a default — answering the whole-FY
        # graph instead would quietly ignore what they asked for.
        return _bad('node_type and node_id must be supplied together')

    seed_findings: list[tuple[str, str]] = []
    if node_type:
        sql, args = WALK_SQL, {
            'fy': fy, 'node_type': node_type, 'node_id': node_id,
            'depth': depth, 'limit': EDGE_CAP + 1,
        }
    else:
        sql, args = ALL_EDGES_SQL, {'fy': fy, 'limit': EDGE_CAP + 1}
        # A finding with no links, no attachments and no check_code produces no
        # edge rows at all, but "every finding in the FY" must still list it.
        finding_qs = AuditFinding.objects.all()
        if fy is not None:
            finding_qs = finding_qs.filter(fy=fy)
        seed_findings = [
            ('finding', str(pk))
            for pk in finding_qs.order_by('ref').values_list('id', flat=True)[:EDGE_CAP + 1]
        ]

    with connection.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()

    truncated = len(rows) > EDGE_CAP or len(seed_findings) > EDGE_CAP
    rows = rows[:EDGE_CAP]
    seed_findings = seed_findings[:EDGE_CAP]

    edges = [
        {'from_type': f_type, 'from_id': f_id, 'edge': edge,
         'to_type': t_type, 'to_id': t_id, 'label': label}
        for f_type, f_id, edge, t_type, t_id, label in rows
    ]

    pairs = list(seed_findings)
    if node_type:
        pairs.append((node_type, node_id))
    for e in edges:
        pairs.append((e['from_type'], e['from_id']))
        pairs.append((e['to_type'], e['to_id']))

    return Response({
        'fy': fy,
        'current_fy': current_fy(),
        'depth': depth,
        'truncated': truncated,
        'nodes': _build_nodes(pairs),
        'edges': edges,
    })
