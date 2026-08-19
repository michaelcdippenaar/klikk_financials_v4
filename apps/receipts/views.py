"""
REST surface for the Audit → Receipts review workflow over the WhatsApp
Slippies register (``whatsapp.klikk_slips``), consumed by the console.

GET   /audit/receipts/                         [JWT] list (filters, ordering, pagination, whole-filter totals)
GET   /audit/receipts/?ids_only=1              [JWT] matching sha256s only — no rows, no pagination
GET   /audit/receipts/<sha256>/                [JWT] one slip + full ocr + items + comments
PATCH /audit/receipts/<sha256>/review/         [JWT] upsert {to_process, decision, note}
POST  /audit/receipts/<sha256>/comments/       [JWT] add {text}
POST  /audit/receipts/bulk/                    [JWT] apply review fields and/or a comment to many sha256s
GET   /audit/receipts/export/?format=csv|xlsx  [JWT] every matching row, no pagination

**Every** endpoint requires an authenticated user (401 otherwise). The project's
DRF default is ``AllowAny``, so each view opts in explicitly: the five DRF views
via ``@permission_classes([IsAuthenticated])`` and the export — a plain Django
view, see ``receipts_export_view`` — via ``@drf_login_required``. Gating the
reads matters because every row carries a ``view_url``: a signed, non-expiring
link to the receipt image (``apps.audit.slip_view``). An open list endpoint
would hand out a valid link for every receipt in the register to any caller,
collapsing the unguessability the signed viewer relies on.

The signed viewer itself stays deliberately public — the console's modal loads
it in a plain ``<img>`` / ``<iframe>``, which cannot carry a Bearer token, and
exported spreadsheets link to it. Its guard is the HMAC in ``?s=``.

The register itself is read-only raw SQL (see ``services``); the only tables
written are ``receipts_slipreview`` / ``receipts_slipcomment``.
"""
import csv
import datetime as dt
import functools
import io
import math

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request as DRFRequest
from rest_framework.response import Response
from rest_framework.settings import api_settings

from .models import DECISION_VALUES, SlipComment, SlipReview
from .services import (
    DEFAULT_PAGE_SIZE, MAX_IDS, MAX_PAGE_SIZE, _bool, attach_review_state, build_filters, comment_to_dict,
    existing_sha256s, query_slip, query_slip_ids, query_slips, query_totals, resolve_ordering, review_to_dict,
    slip_exists,
)

_BODY_NOT_OBJECT = 'request body must be a JSON object'

BULK_MAX = 500  # sha256s per bulk request, counted AFTER de-duplication

EXPORT_COLUMNS = [
    ('slip_ts', 'date'), ('supplier', 'supplier'), ('total', 'total'), ('category', 'category'),
    ('xero_status', 'xero_status'), ('status_group', 'status_group'), ('journal_number', 'journal_number'),
    ('synced_to_xero', 'synced'), ('to_process', 'to_process'), ('decision', 'decision'), ('note', 'note'),
    ('filename', 'filename'), ('sha256', 'sha256'), ('view_url', 'view_url'),
]


def _int(value, default, lo=None, hi=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _username(request) -> str:
    user = getattr(request, 'user', None)
    return (getattr(user, 'get_username', lambda: '')() or '')[:150] if user and user.is_authenticated else ''


def drf_login_required(view):
    """
    ``IsAuthenticated`` for a plain (non-DRF) Django view.

    Runs the project's ``DEFAULT_AUTHENTICATION_CLASSES`` (simplejwt Bearer first,
    then DRF Token, then session) against the incoming request and 401s if none of
    them yields an authenticated user. Used by the export, which cannot be an
    ``@api_view``: DRF content negotiation reads ``?format=`` as a renderer
    override and would 404 on ``csv`` / ``xlsx`` before the view ever ran.

    Sets ``request.user`` on success so the view body sees the same user a DRF
    view would. Only ever applied to GET endpoints — session auth's CSRF
    enforcement is a no-op on safe methods.
    """
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        drf_request = DRFRequest(
            request,
            authenticators=[cls() for cls in api_settings.DEFAULT_AUTHENTICATION_CLASSES],
        )
        try:
            user = drf_request.user
        except APIException:
            user = None  # malformed / expired / revoked credentials -> same 401 as none at all
        if user is None or not user.is_authenticated:
            resp = JsonResponse({'detail': 'Authentication credentials were not provided.'},
                                status=status.HTTP_401_UNAUTHORIZED)
            resp['WWW-Authenticate'] = 'Bearer realm="api"'
            return resp
        request.user = user
        return view(request, *args, **kwargs)

    return wrapper


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def receipts_list_view(request):
    params = request.query_params
    where, args = build_filters(params)
    ordering = resolve_ordering(params.get('ordering'))
    totals = query_totals(where, args)
    if _bool(params.get('ids_only')):
        # Select-all support for the bulk endpoint: the sha256s the CURRENT filter matches, in the
        # current ordering — same build_filters/resolve_ordering as the row mode, but one cheap scan
        # (see query_slip_ids) and no pagination; page/page_size are ignored. Fetching MAX_IDS + 1
        # lets `truncated` distinguish "exactly MAX_IDS" from "more than MAX_IDS".
        ids = query_slip_ids(where, args, ordering, limit=MAX_IDS + 1)
        return Response({'count': totals['count'], 'sha256s': ids[:MAX_IDS], 'truncated': len(ids) > MAX_IDS})
    page = _int(params.get('page'), 1, 1)
    page_size = _int(params.get('page_size'), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    num_pages = max(1, math.ceil(totals['count'] / page_size))
    rows = query_slips(where, args, ordering, limit=page_size, offset=(page - 1) * page_size)
    return Response({
        'count': totals['count'],
        'page': page,
        'page_size': page_size,
        'num_pages': num_pages,
        'totals': totals,
        'results': attach_review_state(rows),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def receipt_detail_view(request, sha256):
    row = query_slip(sha256)
    if row is None:
        return Response({'detail': 'slip not found'}, status=status.HTTP_404_NOT_FOUND)
    attach_review_state([row])
    row['comments'] = [comment_to_dict(c) for c in SlipComment.objects.filter(sha256=sha256)]
    return Response(row)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def receipt_review_view(request, sha256):
    if not slip_exists(sha256):
        return Response({'detail': 'slip not found'}, status=status.HTTP_404_NOT_FOUND)
    data = request.data
    if not isinstance(data, dict):
        # A JSON string/number/list/null parses fine but is not a mapping; `'note' in data` /
        # `data['note']` would raise TypeError -> 500 from pure client input.
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)
    fields = {}
    if 'to_process' in data:
        raw = data['to_process']
        # Same coercion as services._bool (case-insensitive) so the client never gets a 200 while
        # a value like 'TRUE' / 'Yes' / 'ON' is silently stored as False. Unrecognised values are
        # rejected rather than silently coerced; None (explicit clear) stays False.
        flag = _bool(raw)
        if flag is None and raw is not None:
            return Response({'detail': 'to_process must be a boolean (true/false, 1/0, yes/no, on/off)'},
                            status=status.HTTP_400_BAD_REQUEST)
        fields['to_process'] = bool(flag)
    if 'decision' in data:
        decision = str(data['decision'] or '').strip().upper()
        if decision not in DECISION_VALUES:
            return Response({'detail': f'decision must be one of {list(DECISION_VALUES)}'},
                            status=status.HTTP_400_BAD_REQUEST)
        fields['decision'] = decision
    if 'note' in data:
        fields['note'] = str(data['note'] or '')
    if not fields:
        return Response({'detail': 'nothing to update (expected to_process, decision and/or note)'},
                        status=status.HTTP_400_BAD_REQUEST)
    fields['updated_by'] = _username(request)
    review, _created = SlipReview.objects.update_or_create(sha256=sha256, defaults=fields)
    return Response(review_to_dict(review))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def receipt_comments_view(request, sha256):
    if not slip_exists(sha256):
        return Response({'detail': 'slip not found'}, status=status.HTTP_404_NOT_FOUND)
    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)
    text = str(data.get('text') or '').strip()
    if not text:
        return Response({'detail': 'text is required'}, status=status.HTTP_400_BAD_REQUEST)
    comment = SlipComment.objects.create(sha256=sha256, text=text, author=_username(request))
    return Response(comment_to_dict(comment), status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def receipts_bulk_view(request):
    """Apply review fields and/or one comment to up to ``BULK_MAX`` sha256s in a single request."""
    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)

    bad_sha256s = Response({'detail': 'sha256s must be a non-empty list of sha256 strings'},
                           status=status.HTTP_400_BAD_REQUEST)
    raw_shas = data.get('sha256s')
    if not isinstance(raw_shas, list):
        return bad_sha256s
    shas, seen = [], set()
    for x in raw_shas:
        if not isinstance(x, str):
            return bad_sha256s
        sha = str(x).strip()
        if not sha:
            return bad_sha256s
        if sha not in seen:  # de-duplicate preserving order, so `unknown` mirrors the input
            seen.add(sha)
            shas.append(sha)
    if not shas:
        return bad_sha256s
    # The cap applies AFTER de-duplication — 600 copies of one sha256 is one action, not a 400.
    if len(shas) > BULK_MAX:
        return Response({'detail': f'sha256s is limited to {BULK_MAX} per request'},
                        status=status.HTTP_400_BAD_REQUEST)

    fields = {}
    if 'set_to_process' in data:
        raw = data['set_to_process']
        # Same coercion as receipt_review_view / services._bool, for the same reason: a client must
        # never get a 200 while 'TRUE' / 'Yes' / 'ON' was silently stored as False. None stays False.
        flag = _bool(raw)
        if flag is None and raw is not None:
            return Response({'detail': 'set_to_process must be a boolean (true/false, 1/0, yes/no, on/off)'},
                            status=status.HTTP_400_BAD_REQUEST)
        fields['to_process'] = bool(flag)
    if 'decision' in data:
        decision = str(data['decision'] or '').strip().upper()
        if decision not in DECISION_VALUES:
            return Response({'detail': f'decision must be one of {list(DECISION_VALUES)}'},
                            status=status.HTTP_400_BAD_REQUEST)
        fields['decision'] = decision
    if 'note' in data:
        fields['note'] = str(data['note'] or '')
    comment = None
    if 'comment' in data:
        comment = str(data['comment'] or '').strip()
        if not comment:
            return Response({'detail': 'comment must not be empty'}, status=status.HTTP_400_BAD_REQUEST)
    if not fields and comment is None:
        # Key PRESENCE decides, not truthiness — {"note": ""} is a legitimate "clear the note".
        return Response({'detail': 'nothing to do (expected set_to_process, decision, note and/or comment)'},
                        status=status.HTTP_400_BAD_REQUEST)

    # Unknown sha256s are reported, never fatal: the console may act on a stale ids_only snapshot,
    # and failing the whole batch over rows that vanished helps nobody. All-unknown is still a 200.
    known = existing_sha256s(shas)
    unknown = [s for s in shas if s not in known]
    targets = [s for s in shas if s in known]
    username = _username(request)
    updated = commented = 0
    with transaction.atomic():
        if fields:
            # update_or_create per row: upsert semantics matter more than the round-trips here,
            # and BULK_MAX keeps the ceiling at 500.
            defaults = {**fields, 'updated_by': username}
            for sha in targets:
                SlipReview.objects.update_or_create(sha256=sha, defaults=defaults)
                updated += 1
        if comment is not None:
            SlipComment.objects.bulk_create(
                [SlipComment(sha256=sha, text=comment, author=username) for sha in targets]
            )
            commented = len(targets)
    return Response({'updated': updated, 'commented': commented, 'unknown': unknown})


def _export_rows(params):
    where, args = build_filters(params)
    rows = attach_review_state(query_slips(where, args, resolve_ordering(params.get('ordering'))))
    out = []
    for r in rows:
        flat = dict(r)
        flat.update(r['review'])
        out.append([flat.get(key) for key, _ in EXPORT_COLUMNS])
    return [label for _, label in EXPORT_COLUMNS], out


def _export_filename(ext: str) -> str:
    return f'receipts-{dt.date.today().isoformat()}.{ext}'


def _csv_response(headers, rows, note: str | None = None) -> HttpResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    resp = HttpResponse(buf.getvalue(), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{_export_filename("csv")}"'
    if note:
        resp['X-Export-Note'] = note
    return resp


@drf_login_required
@require_GET
def receipts_export_view(request):
    # Plain Django view (not @api_view): DRF content negotiation treats ?format= as a
    # renderer override and would 404 on csv/xlsx before the view ran. Auth is therefore
    # explicit (@drf_login_required, outermost so an anonymous caller never reaches here)
    # rather than inherited from a permission class.
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    if fmt not in ('csv', 'xlsx'):
        return JsonResponse({'detail': 'format must be csv or xlsx'}, status=status.HTTP_400_BAD_REQUEST)
    headers, rows = _export_rows(request.GET)
    if fmt == 'csv':
        return _csv_response(headers, rows)
    try:
        from openpyxl import Workbook
    except ImportError:
        return _csv_response(headers, rows, note='openpyxl unavailable; degraded to csv')
    wb = Workbook()
    ws = wb.active
    ws.title = 'Receipts'
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{_export_filename("xlsx")}"'
    return resp
