"""
Saved cube (pivot) view on an audit finding.

PUT    /audit/findings/<pk>/cube-view/          [auth] save {name?, spec, query?, cube_note?}
DELETE /audit/findings/<pk>/cube-view/          [auth] clear it (cube_note goes with it)
GET    /audit/findings/<pk>/cube-view/data/     [auth] run it, return the cross-tab
GET    /audit/findings/<pk>/cube-view/suggest/  [auth] pre-seed a spec from structured data; never saves

A finding stores the SAME ``{name, spec, query}`` shape the Excel add-in and
``app.cube_views`` (``apps/xero/xero_data/cube_saved.py``) already use — a
senior-dev decision, so exactly one pivot-spec format exists. Nothing here
aggregates anything: ``/data/`` translates the stored spec into the pivot
endpoint's query params (a faithful Python port of ``cubeQueryParams()`` in
``klikk_portal/mcp/stock-market/server.mjs`` — keep the two in sync) and
invokes ``XeroJournalPivotView`` itself with a synthesised GET carrying the
caller's already-authenticated user. If the pivot rejects the spec, its error
body is passed through with its status so the caller sees the real reason.

``/suggest/`` derives ONLY from structured data the finding actually holds —
its ``fy`` and its ``AuditFindingLink`` rows. It never parses the title or
description prose: a guessed supplier would put invented numbers in front of
an auditor. ``derived_from`` names exactly which inputs contributed.

Validation vocabulary (dimension keys, measures, error wording) comes from
``apps/xero/xero_data/pivot_views.py`` — imported, never copied, so a new
dimension is valid here the moment the pivot grows it.
"""
import json
import logging

from django.db import Error as DatabaseError
from django.db.models import Q
from django.test import RequestFactory
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request as DRFRequest
from rest_framework.response import Response

from apps.xero.xero_data.pivot_views import DIMENSIONS, MEASURES, XeroJournalPivotView

from .findings_views import actor
from .models import AuditFinding
from .services import fy_bounds

logger = logging.getLogger(__name__)

_BODY_NOT_OBJECT = 'request body must be a JSON object'
_NO_CUBE_VIEW = 'no cube view saved on this finding'


def clean_spec(raw):
    """Validate and normalise a cube spec into the canonical shape.

    The Python twin of ``cubeSpecFrom()`` in server.mjs, for callers that PUT a
    spec directly rather than through the MCP tool. Error wording matches
    ``XeroJournalPivotView.get`` / ``parse_dim_filters`` exactly, so a caller
    sees one vocabulary whether a spec is rejected at save time or at run time.

    Returns ``(spec, None)`` or ``(None, error_message)``.
    """
    if not isinstance(raw, dict):
        return None, 'spec is required and must be a JSON object'

    rows_in = raw.get('rows')
    if not isinstance(rows_in, list):
        rows_in = []
    rows = [str(r) for r in rows_in if r]
    if not rows:
        return None, 'at least one row dimension is required'

    cols_in = raw.get('cols')
    if cols_in is None:
        cols_in = []
    if not isinstance(cols_in, list):
        return None, 'cols must be a list of dimension keys'
    cols = [str(c) for c in cols_in if c]

    bad = [d for d in rows + cols if d not in DIMENSIONS]
    if bad:
        return None, 'unknown dimension(s): %s' % ', '.join(bad)
    if len(set(rows + cols)) != len(rows + cols):
        return None, 'a dimension cannot appear on both axes'

    measure = str(raw.get('measure') or 'amount').strip()
    if measure not in MEASURES:
        return None, 'unknown measure: %s' % measure

    filters_in = raw.get('filters')
    if filters_in is not None and not isinstance(filters_in, dict):
        return None, 'filters must be a JSON object of dimension -> [values]'
    filters = {}
    for k, v in (filters_in or {}).items():
        if k not in DIMENSIONS:
            # parse_dim_filters' wording: this is exactly where the key would
            # fail at run time, since spec.filters travels as the dimf param.
            return None, 'unknown dimension in dimf: %s' % k
        if isinstance(v, list) and v:
            filters[k] = [str(x) for x in v]

    totals_in = raw.get('totals')
    if totals_in is not None and not isinstance(totals_in, dict):
        return None, 'totals must be a JSON object of dimension -> bool'
    totals = {str(k): bool(v) for k, v in (totals_in or {}).items()}

    return {
        'rows': rows,
        'cols': cols,
        'measure': measure,
        'filt': [k for k in filters if k not in rows and k not in cols],
        'filters': filters,
        'totals': totals,
        'suppress': True if raw.get('suppress') is None else bool(raw.get('suppress')),
        'outline': True if raw.get('outline') is None else bool(raw.get('outline')),
    }, None


def cube_query_params(spec, query):
    """The pivot query params for a spec — the shape the Excel add-in sends.

    Faithful port of ``cubeQueryParams()`` in server.mjs; keep in sync. The
    subtle part is the totals default rule: parent totals are ON for row
    dimensions and OFF for column dimensions unless ``spec.totals`` says
    otherwise. ``rtotals`` is only emitted when at least one row parent is
    switched OFF (absent means "all on", the pivot's historical behaviour), and
    ``__none__`` when every row parent is off — an EMPTY rtotals param would
    read as absent and turn them all back on. ``ctotals`` is only emitted when
    at least one column parent is explicitly ON.
    """
    params = {
        'rows': ','.join(spec.get('rows') or []),
        'cols': ','.join(spec.get('cols') or []),
        'measure': str(spec.get('measure') or 'amount'),
        'suppress': '1' if spec.get('suppress') else '0',
    }

    live = {k: v for k, v in (spec.get('filters') or {}).items()
            if isinstance(v, list) and v}
    if live:
        # Compact separators = JSON.stringify — so the echoed params are
        # byte-identical to what the Excel add-in sends for the same spec.
        params['dimf'] = json.dumps(live, separators=(',', ':'))

    totals = spec.get('totals') or {}

    def on(key, zone):
        if key in totals:
            return bool(totals[key])
        return zone == 'rows'

    row_parents = (spec.get('rows') or [])[:-1]
    if any(not on(k, 'rows') for k in row_parents):
        keep = [k for k in row_parents if on(k, 'rows')]
        params['rtotals'] = ','.join(keep) if keep else '__none__'

    ct = [k for k in (spec.get('cols') or [])[:-1] if on(k, 'cols')]
    if ct:
        params['ctotals'] = ','.join(ct)

    for k, v in (query or {}).items():
        if v is None:
            continue
        # JS String() semantics for the two non-string types that can survive
        # a JSON round trip; str(True) would give 'True', which the pivot's
        # suppress-style checks do not recognise.
        s = ('true' if v else 'false') if isinstance(v, bool) else str(v)
        if s != '':
            params[str(k)] = s
    return params


def _get_finding(pk):
    return AuditFinding.objects.filter(pk=pk).first()


def _no_nul(payload):
    """True when no string anywhere in ``payload`` contains NUL (0x00).

    Same reason as ``findings_views._nul_error``, but the spec/query are
    arbitrarily nested, so the scan runs over the JSON serialisation — a
    ``jsonb`` column rejects ``\\u0000`` just as ``text`` does, and the raise
    would surface as a 500 on pure client input.
    """
    return '\\u0000' not in json.dumps(payload)


@api_view(['PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def finding_cube_view_view(request, pk: int):
    finding = _get_finding(pk)
    if finding is None:
        return Response({'detail': 'finding not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'DELETE':
        had = finding.cube_view is not None
        finding.cube_view = None
        # The note annotates the saved view; it does not outlive it.
        finding.cube_note = ''
        finding.updated_by = actor(request)
        finding.save(update_fields=['cube_view', 'cube_note', 'updated_by', 'updated_at'])
        return Response({'finding_id': finding.pk, 'cleared': had})

    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)

    spec, err = clean_spec(data.get('spec'))
    if err:
        return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)

    query_in = data.get('query')
    if query_in is not None and not isinstance(query_in, dict):
        return Response({'detail': 'query must be a JSON object of journal filters'},
                        status=status.HTTP_400_BAD_REQUEST)
    query = query_in or {}

    name = data.get('name')
    name = (str(name).strip() or None) if name is not None else None

    note = data.get('cube_note')
    if note is not None and not isinstance(note, str):
        return Response({'detail': 'cube_note must be a string'},
                        status=status.HTTP_400_BAD_REQUEST)

    cube_view = {'name': name, 'spec': spec, 'query': query}
    if not _no_nul([cube_view, note]):
        return Response({'detail': 'cube view must not contain NUL (0x00) characters'},
                        status=status.HTTP_400_BAD_REQUEST)

    finding.cube_view = cube_view
    update_fields = ['cube_view', 'updated_by', 'updated_at']
    if 'cube_note' in data:
        finding.cube_note = note or ''
        update_fields.append('cube_note')
    finding.updated_by = actor(request)
    finding.save(update_fields=update_fields)

    return Response({
        'finding_id': finding.pk,
        'fy': finding.fy,
        'name': name,
        'spec': spec,
        'query': query,
        'cube_note': finding.cube_note,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def finding_cube_data_view(request, pk: int):
    finding = _get_finding(pk)
    if finding is None:
        return Response({'detail': 'finding not found'}, status=status.HTTP_404_NOT_FOUND)

    cube_view = finding.cube_view
    if not isinstance(cube_view, dict) or not isinstance(cube_view.get('spec'), dict):
        return Response({'detail': _NO_CUBE_VIEW}, status=status.HTTP_404_NOT_FOUND)

    spec = cube_view.get('spec') or {}
    query = cube_view.get('query') if isinstance(cube_view.get('query'), dict) else {}
    params = cube_query_params(spec, query)

    # Invoke the REAL pivot view — not a copy of its queryset — with a
    # synthesised GET carrying the caller's already-authenticated user.
    # ``view.get`` is called directly: authentication already happened on THIS
    # request, and the pivot's own IsAuthenticated would only re-check it.
    django_request = RequestFactory().get('/xero/data/journals/pivot/', params)
    django_request.user = request.user
    drf_request = DRFRequest(django_request)
    drf_request.user = request.user
    view = XeroJournalPivotView()
    view.request, view.args, view.kwargs, view.format_kwarg = drf_request, (), {}, None
    pivot_response = view.get(drf_request)

    if pivot_response.status_code != status.HTTP_200_OK:
        # The pivot's error body says WHY (unknown dimension, bad dimf, ...);
        # a generic wrapper here would hide the one useful fact.
        return Response(pivot_response.data, status=pivot_response.status_code)

    return Response({
        'finding_id': finding.pk,
        'fy': finding.fy,
        'name': cube_view.get('name'),
        'spec': spec,
        'query': query,
        # Echoed deliberately: how a human debugs a disagreement between this
        # and the Excel add-in — same params, same pivot, same numbers.
        'params': params,
        'cube': pivot_response.data,
    })


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def finding_cube_suggest_view(request, pk: int):
    """Pre-seed a cube spec from the finding's STRUCTURED data. Never saves.

    Contributions (CONTRACT-2 §2, closed list — prose is never parsed):
    - ``fy``            -> query date range from ``fy_bounds`` + a fin_year label filter
    - journal link      -> ``query.q = <journal_number>`` (first journal link; q is one search string)
    - xero_document     -> resolved contact name(s) -> ``filters.supplier``
    - bank_transaction  -> its ``transaction_date`` (never posting_date — unreliable
                           on account 363177001) narrows the date range
    Default layout when nothing else is derivable: supplier x fin_period, amount,
    journal_type=transaction (one mirror, so totals are real figures).
    """
    finding = _get_finding(pk)
    if finding is None:
        return Response({'detail': 'finding not found'}, status=status.HTTP_404_NOT_FOUND)

    derived_from = ['fy']
    date_from, date_to = fy_bounds(finding.fy)
    query = {
        'journal_type': 'transaction',
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
    }
    filters = {'fin_year': ['FY%d' % finding.fy]}

    # Links are additive refinement. The table lands with migration
    # 0003_finding_cube_and_links; until it is applied (or if a resolution
    # target table is missing) the suggestion degrades to the fy-only default
    # rather than failing — a starting point beats a 500.
    try:
        links = list(finding.links.all())
    except DatabaseError:
        logger.warning('finding %s: links unavailable; suggesting from fy only', pk)
        links = []

    journal_refs = [ln.ref for ln in links if ln.kind == 'journal' and ln.ref]
    if journal_refs:
        query['q'] = journal_refs[0]
        derived_from.append('link:journal:%s' % journal_refs[0])
        if len(journal_refs) > 1:
            derived_from.append(
                'note: %d journal links; q can carry one — used the first' % len(journal_refs))

    doc_refs = [ln.ref for ln in links if ln.kind == 'xero_document' and ln.ref]
    if doc_refs:
        try:
            from apps.xero.xero_data.models import XeroDocument
            ids = [i for i in (_int_or_none(r) for r in doc_refs) if i is not None]
            att_ids = [r for r in doc_refs if _int_or_none(r) is None]
            docs = (XeroDocument.objects
                    .filter(Q(pk__in=ids) | Q(xero_attachment_id__in=att_ids))
                    .select_related('transaction_source__contact'))
            names = sorted({
                d.transaction_source.contact.name
                for d in docs
                if d.transaction_source and d.transaction_source.contact
                and d.transaction_source.contact.name
            })
        except DatabaseError:
            names = []
        if names:
            filters['supplier'] = names
            derived_from.append('link:xero_document -> supplier: %s' % ', '.join(names))

    txn_refs = [ln.ref for ln in links if ln.kind == 'bank_transaction' and ln.ref]
    if txn_refs:
        try:
            from apps.investec.models import InvestecBankTransaction
            ids = [i for i in (_int_or_none(r) for r in txn_refs) if i is not None]
            uuids = [r for r in txn_refs if _int_or_none(r) is None]
            dates = sorted(
                InvestecBankTransaction.objects
                .filter(Q(pk__in=ids) | Q(uuid__in=uuids))
                .exclude(transaction_date=None)
                .values_list('transaction_date', flat=True))
        except DatabaseError:
            dates = []
        # NARROW the FY range — intersection, not replacement. A linked txn
        # dated outside the finding's FY (possible; links are human-made)
        # must not drag the range out from under the fin_year filter, which
        # would guarantee an empty cube. If the intersection is empty, the
        # dates cannot narrow anything and the FY range stands.
        if dates:
            new_from = max(date_from, dates[0])
            new_to = min(date_to, dates[-1])
            if new_from <= new_to:
                query['date_from'] = new_from.isoformat()
                query['date_to'] = new_to.isoformat()
                derived_from.append(
                    'link:bank_transaction -> dates %s..%s'
                    % (query['date_from'], query['date_to']))

    spec = {
        'rows': ['supplier'],
        'cols': ['fin_period'],
        'measure': 'amount',
        # filt = filter-well dimensions: filtered but on neither axis. A
        # supplier filter derived from a document link stays OFF this list —
        # supplier is on the rows axis, where the filter subsets the members.
        'filt': [k for k in filters if k not in ('supplier', 'fin_period')],
        'filters': filters,
        'totals': {},
        'suppress': True,
        'outline': True,
    }

    return Response({
        'finding_id': finding.pk,
        'fy': finding.fy,
        'name': None,
        'spec': spec,
        'query': query,
        'derived_from': derived_from,
    })
