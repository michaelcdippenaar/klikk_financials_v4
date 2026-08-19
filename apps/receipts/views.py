"""
REST surface for the Audit → Receipts review workflow over the WhatsApp
Slippies register (``whatsapp.klikk_slips``), consumed by the console.

GET   /audit/receipts/                         list (filters, ordering, pagination, whole-filter totals)
GET   /audit/receipts/<sha256>/                one slip + full ocr + items + comments
PATCH /audit/receipts/<sha256>/review/         [JWT] upsert {to_process, decision, note}
POST  /audit/receipts/<sha256>/comments/       [JWT] add {text}
GET   /audit/receipts/export/?format=csv|xlsx  every matching row, no pagination

Reads keep the project default (AllowAny); writes require an authenticated
user. The register itself is read-only raw SQL (see ``services``); the only
tables written are ``receipts_slipreview`` / ``receipts_slipcomment``.
"""
import csv
import datetime as dt
import io
import math

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import DECISION_VALUES, SlipComment, SlipReview
from .services import (
    DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, attach_review_state, build_filters, comment_to_dict, query_slip,
    query_slips, query_totals, resolve_ordering, review_to_dict, slip_exists,
)

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


@api_view(['GET'])
def receipts_list_view(request):
    params = request.query_params
    page = _int(params.get('page'), 1, 1)
    page_size = _int(params.get('page_size'), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    where, args = build_filters(params)
    totals = query_totals(where, args)
    num_pages = max(1, math.ceil(totals['count'] / page_size))
    rows = query_slips(where, args, resolve_ordering(params.get('ordering')),
                       limit=page_size, offset=(page - 1) * page_size)
    return Response({
        'count': totals['count'],
        'page': page,
        'page_size': page_size,
        'num_pages': num_pages,
        'totals': totals,
        'results': attach_review_state(rows),
    })


@api_view(['GET'])
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
    data = request.data or {}
    fields = {}
    if 'to_process' in data:
        fields['to_process'] = data['to_process'] in (True, 1, '1', 'true', 'True', 'yes', 'on')
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
    text = str((request.data or {}).get('text') or '').strip()
    if not text:
        return Response({'detail': 'text is required'}, status=status.HTTP_400_BAD_REQUEST)
    comment = SlipComment.objects.create(sha256=sha256, text=text, author=_username(request))
    return Response(comment_to_dict(comment), status=status.HTTP_201_CREATED)


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


@require_GET
def receipts_export_view(request):
    # Plain Django view (not @api_view): DRF content negotiation treats ?format= as a
    # renderer override and would 404 on csv/xlsx before the view ran.
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
