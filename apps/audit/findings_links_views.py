"""
Linked evidence on an audit finding — typed pointers to entities elsewhere in the stack.

POST   /audit/findings/<pk>/links/     [auth] {kind, ref, label?} -> 201; duplicate -> 200 + created:false
GET    /audit/findings/<pk>/links/     [auth] list, every link resolved for display
DELETE /audit/findings/links/<pk>/     [auth] remove one link

Design notes (why, not what):

* A link's ``ref`` points at tables this app does not own (``whatsapp.klikk_slips``,
  the Xero document / journal / invoice mirrors, ``investec_investecbanktransaction``)
  or at Asana, which is not in this database at all. A ref that resolves to nothing is
  therefore NORMAL — a slip can be purged, a document re-synced under a new id — and
  resolution degrades to ``{"found": false, "title": <raw ref>}`` instead of erroring.
  One stale link must never 500 the whole finding modal.

* Resolution is BATCHED: a constant number of queries per KIND present in the list
  (one for most kinds, two for ``xero_document`` because the contact/date subtitle
  lives on the invoice joined via the transaction source), never one query per link.
  A finding with 20 slip links costs the same as one with a single slip link.

* Signed viewers are REUSED, never re-implemented: slips through
  ``apps.audit.slip_view.slip_url`` and Xero documents through
  ``apps.xero.xero_data.document_views.xero_document_url``. Both links are
  deliberately unauthenticated (an ``<img>``/``<iframe>`` cannot carry a Bearer
  token) with the HMAC in ``?s=`` as their only guard — which is why every endpoint
  in this module requires an authenticated caller: an open list would hand out valid
  signed links for every cited slip/document to any caller.

* Bank transactions: credits are stored NEGATIVE in ``investec_investecbanktransaction``
  (documented system fact), and ``posting_date`` is unreliable (on account 363177001
  every row carries the backfill date 2026-05-28) — so display always uses
  ``transaction_date`` and the stored signed amount plus the CREDIT/DEBIT type.

* Journals: one ``journal_number`` is MANY lines in ``xero_data_xerojournals`` and can
  even span more than one ``journal_id`` (numbers are only unique per organisation /
  journal type — see apps/receipts/services.py). Blindly summing lines double-counts,
  and the signed line amounts net to zero by construction, so the summary aggregates
  once per number: line count, distinct-journal count, total DEBIT side as the
  magnitude, and one narration — never a dump of every line.

* ``asana`` refs are never looked up (no Asana API call from the backend — a new
  outbound call would be a sub-processor decision, not a display concern); the title
  is the stored label or the gid and the URL is composed locally.

This module must not import from ``findings_views`` (CHUNK B): that module imports
``resolve_links`` / ``link_counts_for`` from here for the detail payload and the list
``link_count``, so the tiny ``actor()`` identity rule is duplicated below instead of
imported — same behaviour, no import cycle.
"""
import re

from django.db import IntegrityError, connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django.db.models import Count, Max, Min, Q, Sum

from klikk_business_intelligence.permissions import ServiceAccount

from apps.audit.slip_view import slip_url
from apps.investec.models import InvestecBankTransaction
from apps.receipts.services import SLIP_TZ, SUPPLIER_SQL, TOTAL_SQL
from apps.xero.xero_data.document_views import xero_document_url
from apps.xero.xero_data.models import XeroDocument, XeroInvoice, XeroJournals

from apps.activity import models as A
from apps.activity.services import record_activity

from .comment_events import finding_ref
from .models import LINK_KINDS, AuditFinding, AuditFindingLink

_BODY_NOT_OBJECT = 'request body must be a JSON object'

REF_MAX_LENGTH = 128    # AuditFindingLink.ref
LABEL_MAX_LENGTH = 300  # AuditFindingLink.label

ASANA_URL = 'https://app.asana.com/0/0/{gid}'

# --------------------------------------------------------------------------- #
# Tenant-qualified refs (journal / invoice)
#
# Xero numbers both journals and invoices PER ORGANISATION, so the number alone
# does not identify a row: measured on this database, 31,071 journal_numbers and
# 448 invoice_numbers occur in more than one organisation (journal numbers are
# sequential per tenant, so 1, 2, 3 ... exist three times over). Resolving a bare
# number therefore has to pick a winner, and any rule for picking one is a silent
# lie about which entity's document an auditor is looking at.
#
# So these two kinds store a QUALIFIED ref, "<tenant_uuid>:<number>". A bare
# number is accepted on input and canonicalised to the Klikk tenant, which keeps
# older links and casual agent calls working while making the stored value
# unambiguous. Doing this now is deliberate: changing the ref format after MC has
# saved links would mean migrating rows whose meaning we could no longer recover.
# --------------------------------------------------------------------------- #

KLIKK_TENANT_ID = '41ebfa0e-012e-4ff1-82ba-a9a7585c536c'
TENANT_QUALIFIED_KINDS = ('journal', 'invoice')

_TENANT_REF_RE = re.compile(
    r'^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):(.+)$'
)


def split_tenant_ref(ref: str) -> tuple[str, str]:
    """('<tenant_uuid>', '<number>'). A bare number is read as the Klikk tenant."""
    ref = (ref or '').strip()
    m = _TENANT_REF_RE.match(ref)
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return KLIKK_TENANT_ID, ref


def canonical_ref(kind: str, ref: str) -> str:
    """Storage form of a ref. Only journal/invoice are qualified; others pass through."""
    if kind not in TENANT_QUALIFIED_KINDS:
        return ref
    tenant, number = split_tenant_ref(ref)
    return f'{tenant}:{number}'


def _tenant_names() -> dict:
    """{tenant_uuid: name} — one query, so a resolved title can name the entity."""
    with connection.cursor() as cur:
        cur.execute('SELECT tenant_id, tenant_name FROM xero_core_xerotenant')
        return {str(r[0]): (r[1] or '') for r in cur.fetchall()}


def actor(request) -> str:
    """Who to stamp on ``added_by`` — same rule as the rest of the register.

    Service-token callers (the MCP server, agents) authenticate as the shared
    ``ServiceAccount`` stand-in and are recorded as ``'mcp'``; humans get their
    username. Duplicated from findings_views (see module docstring: import cycle).
    """
    user = getattr(request, 'user', None)
    if isinstance(user, ServiceAccount):
        return 'mcp'
    if user is not None and user.is_authenticated:
        return (user.get_username() or '')[:150]
    return ''


def _nul_error(field: str, *values: str):
    """400 if any of ``values`` contains NUL (0x00), else None.

    JSON permits ``\\u0000`` but a Postgres ``text`` column cannot store it: psycopg
    raises while binding, so an unguarded write turns pure client input into a raw
    500 — same guard as apps/receipts/views.py::_nul_error.
    """
    if any('\x00' in v for v in values):
        return Response({'detail': f'{field} must not contain NUL (0x00) characters'},
                        status=status.HTTP_400_BAD_REQUEST)
    return None


def _amount_str(value) -> str | None:
    """A Decimal/float as a 2dp plain string ('1234.50'), or None."""
    return None if value is None else f'{value:.2f}'


def _rand(value) -> str:
    """Display money: 'R1,204.50' (negatives keep their sign: 'R-500.00')."""
    return f'R{value:,.2f}'


def _int_refs(refs):
    """The subset of ``refs`` that parse as ints, as ints. Non-numeric refs simply
    can't match an integer key — they resolve to found:false, never to an error."""
    out = []
    for r in refs:
        try:
            out.append(int(r))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# Per-kind batched resolvers. Each takes the distinct refs of that kind in the
# list being resolved and returns {ref: resolved_dict}. A ref absent from the
# result map is rendered as found:false by resolve_links().
# --------------------------------------------------------------------------- #

def _resolve_slips(refs):
    sql = (
        f'select s.sha256, {SUPPLIER_SQL}, {TOTAL_SQL}, s.slip_ts, s.filename '
        'from whatsapp.klikk_slips s where s.sha256 = any(%s)'
    )
    with connection.cursor() as cur:
        cur.execute(sql, [list(refs)])
        rows = cur.fetchall()
    out = {}
    for sha256, supplier, total, slip_ts, filename in rows:
        slip_date = slip_ts.astimezone(SLIP_TZ).date().isoformat() if slip_ts else None
        title = supplier or filename or sha256
        if supplier and total is not None:
            title = f'{supplier} {_rand(total)}'
        out[sha256] = {
            'found': True,
            'title': title,
            'subtitle': slip_date or '',
            'view_url': slip_url(sha256),
            'detail': {
                'sha256': sha256,
                'supplier': supplier or '',
                'total': _amount_str(total),
                'date': slip_date,
                'filename': filename or '',
            },
        }
    return out


def _resolve_xero_documents(refs):
    docs = list(
        XeroDocument.objects.filter(id__in=_int_refs(refs)).select_related('transaction_source')
    )
    # Second (still batched) query: the subtitle's contact/date live on the invoice
    # joined via transaction_source.transactions_id == XeroInvoice.invoice_id.
    guids = {d.transaction_source.transactions_id for d in docs if d.transaction_source_id}
    invoices = {
        inv.invoice_id: inv
        for inv in XeroInvoice.objects.filter(invoice_id__in=guids)
    } if guids else {}
    out = {}
    for doc in docs:
        inv = invoices.get(doc.transaction_source.transactions_id) if doc.transaction_source_id else None
        subtitle_bits = []
        if inv is not None:
            if inv.contact_name:
                subtitle_bits.append(inv.contact_name)
            if inv.date:
                subtitle_bits.append(inv.date.isoformat())
        out[str(doc.id)] = {
            'found': True,
            'title': doc.file_name,
            'subtitle': ' · '.join(subtitle_bits),
            'view_url': xero_document_url(doc.id),
            'detail': {
                'id': doc.id,
                'file_name': doc.file_name,
                'content_type': doc.content_type or '',
                'invoice_number': inv.invoice_number if inv else '',
                'contact_name': inv.contact_name if inv else '',
                'date': inv.date.isoformat() if inv and inv.date else None,
            },
        }
    return out


def _resolve_bank_transactions(refs):
    int_refs = _int_refs(refs)
    txns = (
        InvestecBankTransaction.objects
        .filter(Q(id__in=int_refs) | Q(uuid__in=list(refs)))
        .select_related('account')
    )
    out = {}
    for t in txns:
        account = t.account
        account_label = f'{account.account_number} {account.account_name or account.reference_name}'.strip()
        # transaction_date, not posting_date (posting_date is a known-unreliable
        # backfill artefact on account 363177001); amount keeps its stored sign —
        # credits are negative in this table.
        txn_date = t.transaction_date.isoformat() if t.transaction_date else None
        resolved = {
            'found': True,
            'title': t.description or f'{t.type} {_rand(t.amount)}',
            'subtitle': ' · '.join(x for x in [txn_date, _rand(t.amount), account_label, t.type] if x),
            'view_url': None,  # there is no file behind a bank transaction
            'detail': {
                'id': t.id,
                'uuid': t.uuid or '',
                'type': t.type,
                'transaction_type': t.transaction_type or '',
                'amount': _amount_str(t.amount),
                'transaction_date': txn_date,
                'account_number': account.account_number,
                'account_name': account.account_name or account.reference_name or '',
                'description': t.description or '',
            },
        }
        # Index under BOTH keys so a link stored by pk and one stored by uuid resolve.
        out[str(t.id)] = resolved
        if t.uuid:
            out[t.uuid] = resolved
    return out


def _resolve_journals(refs):
    """Batched, tenant-aware. ONE query regardless of how many refs are passed."""
    pairs = {ref: split_tenant_ref(ref) for ref in refs}
    tenants = {t for t, _ in pairs.values()}
    numbers = _int_refs({n for _, n in pairs.values()})
    if not numbers:
        return {}
    names = _tenant_names()
    rows = (
        XeroJournals.objects
        .filter(organisation_id__in=tenants, journal_number__in=numbers)
        .values('organisation_id', 'journal_number')
        .annotate(
            line_count=Count('id'),
            journal_count=Count('journal_id', distinct=True),
            total_debit=Sum('debit'),
            first_date=Min('date'),
            narration=Max('description'),
        )
    )
    out = {}
    for row in rows:
        tenant = str(row['organisation_id'])
        number = row['journal_number']
        date = row['first_date'].date().isoformat() if row['first_date'] else None
        debit = row['total_debit'] or 0
        org = names.get(tenant, tenant[:8])
        bits = [b for b in [date, org, f"{row['line_count']} lines", _rand(debit)] if b]
        if row['journal_count'] > 1:
            # Still more than one journal_id WITHIN one organisation (journal types
            # repeat a number) — say so rather than pretending it is a single journal.
            bits.append(f"{row['journal_count']} journals")
        out[f'{tenant}:{number}'] = {
            'found': True,
            'title': row['narration'] or f'Journal {number}',
            'subtitle': ' \u00b7 '.join(bits),
            'view_url': None,
            'detail': {
                'tenant_id': tenant,
                'tenant_name': org,
                'journal_number': number,
                'line_count': row['line_count'],
                'journal_count': row['journal_count'],
                'total_debit': _amount_str(debit),
                'date': date,
            },
        }
    return out


def _resolve_invoices(refs):
    """Batched, tenant-aware. ONE query; keyed by the qualified ref, so an invoice
    number that exists in two organisations resolves to the right one instead of
    whichever row happened to sort first."""
    pairs = {ref: split_tenant_ref(ref) for ref in refs}
    tenants = {t for t, _ in pairs.values()}
    numbers = {n for _, n in pairs.values()}
    if not numbers:
        return {}
    names = _tenant_names()
    out = {}
    qs = XeroInvoice.objects.filter(organisation_id__in=tenants, invoice_number__in=numbers)
    for inv in qs:
        tenant = str(inv.organisation_id)
        date = inv.date.isoformat() if inv.date else None
        org = names.get(tenant, tenant[:8])
        out[f'{tenant}:{inv.invoice_number}'] = {
            'found': True,
            'title': (f'{inv.invoice_number} \u2014 {inv.contact_name}'
                      if inv.contact_name else inv.invoice_number),
            'subtitle': ' \u00b7 '.join(x for x in [date, org, _rand(inv.total)] if x),
            'view_url': None,
            'detail': {
                'tenant_id': tenant,
                'tenant_name': org,
                'invoice_number': inv.invoice_number,
                'contact_name': inv.contact_name or '',
                'date': date,
                'total': _amount_str(inv.total),
                'type': inv.type,
                'status': inv.status,
            },
        }
    return out


def _resolve_asana(link):
    """No lookup — the backend never calls the Asana API (a new outbound call is a
    POPIA §21 sub-processor decision, not a display concern). Always renderable."""
    return {
        'found': True,
        'title': link.label or link.ref,
        'subtitle': '',
        'view_url': ASANA_URL.format(gid=link.ref),
        'detail': {'gid': link.ref},
    }


_RESOLVERS = {
    'slip': _resolve_slips,
    'xero_document': _resolve_xero_documents,
    'bank_transaction': _resolve_bank_transactions,
    'journal': _resolve_journals,
    'invoice': _resolve_invoices,
    # 'asana' is per-link (uses the stored label) and needs no query — handled inline.
}


def resolve_links(links) -> list[dict]:
    """The serialised, display-resolved form of an iterable of AuditFindingLink rows.

    Batched: refs are grouped by kind and each kind's resolver runs ONCE for the whole
    list — query cost is O(kinds present), not O(links). Wired into the finding detail
    payload (``links``) by findings_views; also used directly by the /links/ endpoints.
    """
    links = list(links)
    refs_by_kind: dict[str, set] = {}
    for link in links:
        if link.kind in _RESOLVERS:
            refs_by_kind.setdefault(link.kind, set()).add(link.ref)
    resolved_by_kind = {
        kind: _RESOLVERS[kind](refs) for kind, refs in refs_by_kind.items()
    }
    out = []
    for link in links:
        if link.kind == 'asana':
            resolved = _resolve_asana(link)
        else:
            resolved = resolved_by_kind.get(link.kind, {}).get(link.ref)
            if resolved is None:
                # Dangling ref (purged slip, re-synced document, unknown kind row from
                # an older schema): degrade to found:false, NEVER an error.
                resolved = {'found': False, 'title': link.ref}
        out.append({
            'id': link.id,
            'finding_id': link.finding_id,
            'kind': link.kind,
            'ref': link.ref,
            'label': link.label,
            'added_by': link.added_by,
            'created_at': link.created_at.isoformat() if link.created_at else None,
            'resolved': resolved,
        })
    return out


def link_counts_for(finding_ids) -> dict[int, int]:
    """{finding_id: link_count} for the ids given — one grouped query, for the list
    dict's ``link_count`` (ids with no links are simply absent; callers .get(id, 0))."""
    rows = (
        AuditFindingLink.objects
        .filter(finding_id__in=list(finding_ids))
        .values('finding_id')
        .annotate(n=Count('id'))
    )
    return {row['finding_id']: row['n'] for row in rows}


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def finding_links_view(request, pk: int):
    """GET: the finding's links, resolved. POST: add one — idempotent on (kind, ref)."""
    finding = AuditFinding.objects.filter(pk=pk).first()
    if finding is None:
        return Response({'detail': 'finding not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        links = resolve_links(finding.links.all())
        return Response({'finding_id': finding.id, 'count': len(links), 'links': links})

    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)

    kind = data.get('kind')
    if not isinstance(kind, str) or kind not in LINK_KINDS:
        return Response(
            {'detail': f"kind must be one of: {', '.join(LINK_KINDS)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    ref = data.get('ref')
    if isinstance(ref, int) and not isinstance(ref, bool):
        ref = str(ref)  # agents legitimately send journal numbers / txn pks as numbers
    if not isinstance(ref, str) or not ref.strip():
        return Response({'detail': 'ref is required and must be a non-empty string'},
                        status=status.HTTP_400_BAD_REQUEST)
    ref = ref.strip()
    # Qualify journal/invoice refs with their tenant BEFORE the length check and
    # before the idempotency lookup, so "171" and "<klikk-uuid>:171" are one link.
    ref = canonical_ref(kind, ref)
    if len(ref) > REF_MAX_LENGTH:
        return Response({'detail': f'ref is limited to {REF_MAX_LENGTH} characters'},
                        status=status.HTTP_400_BAD_REQUEST)

    label = data.get('label', '')
    if label is None:
        label = ''
    if not isinstance(label, str):
        return Response({'detail': 'label must be a string'}, status=status.HTTP_400_BAD_REQUEST)
    label = label.strip()
    if len(label) > LABEL_MAX_LENGTH:
        return Response({'detail': f'label is limited to {LABEL_MAX_LENGTH} characters'},
                        status=status.HTTP_400_BAD_REQUEST)

    err = _nul_error('kind/ref/label', kind, ref, label)
    if err is not None:
        return err

    existing = AuditFindingLink.objects.filter(finding=finding, kind=kind, ref=ref).first()
    if existing is not None:
        return Response({'created': False, 'link': resolve_links([existing])[0]})

    try:
        link = AuditFindingLink.objects.create(
            finding=finding, kind=kind, ref=ref, label=label, added_by=actor(request),
        )
    except IntegrityError:
        # Lost a race with a concurrent identical POST — idempotency still holds.
        existing = AuditFindingLink.objects.get(finding=finding, kind=kind, ref=ref)
        return Response({'created': False, 'link': resolve_links([existing])[0]})

    record_activity(request, A.LINK_ADDED, target_kind='link', target_id=link.pk,
                    target_ref=finding_ref(finding),
                    changes={'finding_id': finding.pk, 'kind': kind, 'ref': ref, 'label': label})
    return Response({'created': True, 'link': resolve_links([link])[0]},
                    status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def finding_link_delete_view(request, pk: int):
    link = AuditFindingLink.objects.filter(pk=pk).first()
    if link is None:
        return Response({'detail': 'link not found'}, status=status.HTTP_404_NOT_FOUND)
    # Captured BEFORE the delete — afterwards there is nothing left to describe.
    removed = {'finding_id': link.finding_id, 'kind': link.kind, 'ref': link.ref,
               'label': link.label}
    removed_ref = finding_ref(link.finding)
    link.delete()
    record_activity(request, A.LINK_REMOVED, target_kind='link', target_id=pk,
                    target_ref=removed_ref, changes=removed)
    return Response({'deleted': True, 'id': pk})
