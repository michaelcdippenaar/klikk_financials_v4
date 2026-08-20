"""
REST surface for the Audit Findings register (``audit_auditfinding`` and friends),
consumed by the console's Audit Findings page and the klikk-financials MCP tools.

GET   /audit/findings/                          [auth] list (filters, ordering, pagination, whole-filter totals)
POST  /audit/findings/                          [auth] create — ref auto-allocated per FY, never client-supplied
GET   /audit/findings/summary/                  [auth] aggregates for the console strip (same filters as the list)
GET   /audit/findings/export/?format=csv|xlsx   [auth] every matching row, no pagination
POST  /audit/findings/bulk/                     [auth] status/owner/due_date and/or a comment on many findings
GET   /audit/findings/<pk>/                     [auth] finding + comments + attachments
PATCH /audit/findings/<pk>/                     [auth] update (fy/ref immutable)
POST  /audit/findings/<pk>/comments/            [auth] add {text}
GET   /audit/findings/<pk>/attachments/         [auth] list attachments
POST  /audit/findings/<pk>/attachments/         [auth] multipart upload, field ``file`` (+ optional ``note``) — allowlisted types, 25 MB cap
DELETE /audit/findings/attachments/<pk>/        [auth] delete the row AND remove the file from disk

**Every** endpoint requires an authenticated caller (401 otherwise): the DRF views
via ``@permission_classes([IsAuthenticated])`` and the export — a plain Django view,
see ``findings_export_view`` — via ``@drf_login_required``. Gating the reads matters
because every attachment dict carries a ``view_url``: a signed, non-expiring link to
the file (``findings_file_view``). An open list endpoint would hand out a valid link
for every attachment to any caller, collapsing the unguessability the signed viewer
relies on — same reasoning as ``apps/receipts/views.py``.

Validation and query building live in ``findings_services``; this module only
orchestrates requests, identity stamping, and response shapes.
"""
import csv
import datetime as dt
import functools
import io
import logging
import math
import os
import posixpath

from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.exceptions import APIException
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request as DRFRequest
from rest_framework.response import Response
from rest_framework.settings import api_settings

from klikk_business_intelligence.permissions import ServiceAccount

from .findings_links_views import resolve_links
from .findings_services import (
    BULK_MAX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, allocate_ref, attachment_to_dict, build_queryset,
    clean_payload, comment_to_dict, current_fy, finding_to_dict, fy_options, summary_for, totals_for,
)
from .models import (
    AuditFinding, AuditFindingAttachment, AuditFindingComment, sanitise_attachment_filename,
)

logger = logging.getLogger(__name__)

DETAIL_LINK_LIMIT = 200  # links embedded in the detail payload; see finding_detail_view

_BODY_NOT_OBJECT = 'request body must be a JSON object'

ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024

# PATCH-able keys. fy and ref are deliberately absent: a finding's identity (its FY and its
# ref within that FY) never changes once allocated — supplying them is ignored, not a 400,
# so an MCP caller can round-trip a finding dict without stripping fields first.
EDITABLE_FIELDS = (
    'status', 'owner', 'due_date', 'amount', 'severity', 'category', 'title',
    'description', 'evidence', 'check_code', 'asana_gid', 'source', 'currency',
)

EXPORT_COLUMNS = [
    'ref', 'fy', 'title', 'severity', 'status', 'category', 'amount', 'currency', 'owner',
    'due_date', 'source', 'check_code', 'asana_gid', 'description', 'evidence', 'comments',
    'created_by', 'created_at', 'updated_at',
]


def actor(request) -> str:
    """Who to stamp on created_by / updated_by / comment author.

    Service-token callers (the MCP server, agents) all authenticate as the same
    ``ServiceAccount`` stand-in, recorded as ``'mcp'``; humans get their username.
    """
    user = getattr(request, 'user', None)
    if isinstance(user, ServiceAccount):
        return 'mcp'
    if user is not None and user.is_authenticated:
        return (user.get_username() or '')[:150]
    return ''


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


def _nul_error(field: str, *values: str):
    """
    400 if any of ``values`` contains NUL (0x00), else None.

    Same guard, same reason as ``apps/receipts/views.py::_nul_error``: JSON permits
    ``\\u0000`` but a Postgres ``text`` column cannot store it — psycopg raises while
    binding the parameter, turning pure client input into a raw 500.
    """
    if any('\x00' in v for v in values):
        return Response({'detail': f'{field} must not contain NUL (0x00) characters'},
                        status=status.HTTP_400_BAD_REQUEST)
    return None


def drf_login_required(view):
    """
    ``IsAuthenticated`` for a plain (non-DRF) Django view.

    Duplicated from ``apps/receipts/views.py::drf_login_required`` rather than imported:
    the receipts app is an unrelated feature surface and a cross-app import would couple
    this register's auth path to its module layout — if receipts is ever refactored or
    removed, the findings export must not break with it. Keep the two in sync.

    Runs the project's ``DEFAULT_AUTHENTICATION_CLASSES`` (service token first, then
    simplejwt Bearer, then session) against the incoming request and 401s if none of
    them yields an authenticated user. Used by the export, which cannot be an
    ``@api_view``: DRF content negotiation reads ``?format=`` as a renderer override
    and would 404 on ``csv`` / ``xlsx`` before the view ever ran.

    Sets ``request.user`` on success so the view body sees the same user a DRF view
    would. Only ever applied to GET endpoints — session auth's CSRF enforcement is a
    no-op on safe methods.
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


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def findings_view(request):
    if request.method == 'POST':
        return _create_finding(request)
    qs, meta = build_queryset(request.query_params)
    if meta['errors']:
        return Response({'detail': meta['errors'][0]}, status=status.HTTP_400_BAD_REQUEST)
    totals = totals_for(qs)  # BEFORE the count annotations: keep the aggregate on the flat queryset
    page_size = _int(request.query_params.get('page_size'), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    num_pages = max(1, math.ceil(totals['count'] / page_size))
    # Out-of-range page numbers clamp (contract: never a 400) — including the high end, so a
    # pager sitting on page 3 when rows shrink to one page gets page 1 back, not an empty page.
    page = _int(request.query_params.get('page'), 1, 1, num_pages)
    # finding_to_dict falls back to per-row .count() queries without these annotations —
    # 2 x page_size extra queries per page. distinct=True because the two joins multiply.
    qs = qs.annotate(comment_count=Count('comments', distinct=True),
                     attachment_count=Count('attachments', distinct=True))
    rows = qs[(page - 1) * page_size:page * page_size]
    return Response({
        'count': totals['count'],
        'page': page,
        'page_size': page_size,
        'num_pages': num_pages,
        'fy': meta['fy'],
        'current_fy': current_fy(),
        'totals': totals,
        'results': [finding_to_dict(f) for f in rows],
    })


def _create_finding(request):
    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)
    fields, err = clean_payload(data, partial=False)
    if err:
        return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)
    fields.pop('ref', None)  # ref is allocated, never client-supplied
    fy = fields.pop('fy', None) or current_fy()
    who = actor(request)
    finding = None
    # allocate_ref serialises concurrent creates per FY with an advisory xact lock; the unique
    # (fy, ref) constraint is the backstop. On the (theoretical) IntegrityError the whole
    # transaction is retried — a retry INSIDE the aborted transaction would be a dead end.
    for attempt in range(3):
        try:
            with transaction.atomic():
                ref = allocate_ref(fy)
                finding = AuditFinding.objects.create(fy=fy, ref=ref, created_by=who, **fields)
            break
        except IntegrityError:
            if attempt == 2:
                raise
    return Response(finding_to_dict(finding), status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def finding_detail_view(request, pk: int):
    finding = AuditFinding.objects.filter(pk=pk).first()
    if finding is None:
        return Response({'detail': 'finding not found'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'GET':
        # Links are CAPPED here. The resolved form costs one query per link KIND, but
        # the payload itself is O(links) and a finding can legitimately accumulate
        # thousands (load testing put 6,000 on a single row). Serialising all of them
        # on every modal open is not something the console should ever have to survive,
        # so the detail carries a page of them plus the true total, and the full list
        # lives behind GET /audit/findings/<pk>/links/.
        all_links = list(finding.links.all())
        shown = all_links[:DETAIL_LINK_LIMIT]
        return Response({
            'finding': finding_to_dict(finding),
            'comments': [comment_to_dict(c) for c in finding.comments.all()],
            'attachments': [attachment_to_dict(a) for a in finding.attachments.all()],
            'links': resolve_links(shown),
            'link_count': len(all_links),
            'links_truncated': len(all_links) > DETAIL_LINK_LIMIT,
        })
    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)
    if not any(k in data for k in EDITABLE_FIELDS):
        return Response(
            {'detail': f'nothing to update (expected one of: {", ".join(EDITABLE_FIELDS)})'},
            status=status.HTTP_400_BAD_REQUEST)
    data = {k: v for k, v in data.items() if k in EDITABLE_FIELDS}
    fields, err = clean_payload(data, partial=True)
    if err:
        return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)
    fields.pop('fy', None)
    fields.pop('ref', None)
    fields['updated_by'] = actor(request)
    with transaction.atomic():
        for key, value in fields.items():
            setattr(finding, key, value)
        finding.save()
    return Response(finding_to_dict(finding))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def finding_comments_view(request, pk: int):
    finding = AuditFinding.objects.filter(pk=pk).first()
    if finding is None:
        return Response({'detail': 'finding not found'}, status=status.HTTP_404_NOT_FOUND)
    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)
    text = str(data.get('text') or '').strip()
    if not text:
        return Response({'detail': 'text is required'}, status=status.HTTP_400_BAD_REQUEST)
    bad_nul = _nul_error('text', text)
    if bad_nul is not None:
        return bad_nul
    with transaction.atomic():
        comment = AuditFindingComment.objects.create(finding=finding, text=text, author=actor(request))
    return Response(comment_to_dict(comment), status=status.HTTP_201_CREATED)


# --------------------------------------------------------------------------- #
# Attachments (CONTRACT-2 §1). Uploads are allowlisted on BOTH the extension and
# the client-declared content type, and the two must AGREE — the content type is
# attacker-controlled, so it is never trusted on its own.
# --------------------------------------------------------------------------- #
ATTACHMENT_ALLOWED_TYPES = {
    'pdf': ('application/pdf',),
    'png': ('image/png',),
    'jpg': ('image/jpeg',),
    'jpeg': ('image/jpeg',),
    'xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',),
    'csv': ('text/csv', 'application/csv', 'text/plain'),
    'docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',),
}
ATTACHMENT_ALLOWED_SUMMARY = (
    'pdf (application/pdf), png (image/png), jpg/jpeg (image/jpeg), '
    'xlsx (openxmlformats spreadsheet), csv (text/csv), docx (openxmlformats document)'
)
# Disk-leaf length cap: the FileField's max_length (100) covers the WHOLE relative path,
# including the audit_findings/<fy>/<finding_id>/ prefix and any -N dedup suffix.
ATTACHMENT_MAX_NAME = 64

# CONTRACT-2 §1 asks for an optional ``note`` on upload AND says the model gains nothing
# beyond the storage path — and the model shipped without a ``note`` column. Feature-detect
# so the note starts persisting the moment the column lands (CHUNK M); until then it is
# validated and dropped, which is flagged in the chunk report rather than invented here.
_ATTACHMENT_HAS_NOTE = any(f.name == 'note' for f in AuditFindingAttachment._meta.concrete_fields)


def _leaf_name(raw: str) -> str:
    """Basename across BOTH separator conventions — the client may be Windows."""
    return posixpath.basename((raw or '').replace('\\', '/'))


def _attachment_type_error(name: str, content_type: str):
    """Allowlist check on extension + declared content type. None when acceptable."""
    ext = os.path.splitext(_leaf_name(name))[1].lstrip('.').lower()
    ctype = (content_type or '').split(';')[0].strip().lower()
    allowed = ATTACHMENT_ALLOWED_TYPES.get(ext)
    if not ext or allowed is None:
        shown = f'".{ext}"' if ext else 'missing'
        return (f'file extension {shown} is not allowed — allowed: {ATTACHMENT_ALLOWED_SUMMARY} '
                '(extension and content type must match)')
    if ctype not in allowed:
        return (f'content type "{ctype or "(none)"}" does not match extension ".{ext}" '
                f'(expected {" or ".join(allowed)}) — allowed: {ATTACHMENT_ALLOWED_SUMMARY}')
    return None


def _attachment_disk_name(finding, raw_name: str) -> str:
    """Safe, collision-free leaf for MEDIA_ROOT/audit_findings/<fy>/<finding_id>/.

    Sanitised to a single path segment via models.sanitise_attachment_filename (shared with
    the upload_to callable, so re-sanitising there is idempotent), length-capped, then
    deduplicated with a -2/-3 suffix rather than ever overwriting. Storage's own
    get_available_name remains the race backstop — it never overwrites either.
    """
    leaf = sanitise_attachment_filename(_leaf_name(raw_name))
    stem, ext = os.path.splitext(leaf)
    ext = ext[:16].lower()
    stem = stem[:max(1, ATTACHMENT_MAX_NAME - len(ext))]
    base_dir = posixpath.join('audit_findings', str(finding.fy), str(finding.pk))
    candidate, n = f'{stem}{ext}', 1
    while default_storage.exists(posixpath.join(base_dir, candidate)):
        n += 1
        candidate = f'{stem}-{n}{ext}'
    return candidate


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def finding_attachments_view(request, pk: int):
    finding = AuditFinding.objects.filter(pk=pk).first()
    if finding is None:
        return Response({'detail': 'finding not found'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'GET':
        attachments = [attachment_to_dict(a) for a in finding.attachments.all()]
        return Response({'finding_id': finding.pk, 'count': len(attachments),
                         'attachments': attachments})
    upload = request.FILES.get('file')
    if upload is None:
        return Response({'detail': 'file is required (multipart field "file")'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not upload.size:
        return Response({'detail': 'file is empty (zero bytes)'}, status=status.HTTP_400_BAD_REQUEST)
    if upload.size > ATTACHMENT_MAX_BYTES:
        return Response({'detail': 'file is limited to 25 MB'}, status=status.HTTP_400_BAD_REQUEST)
    type_err = _attachment_type_error(upload.name or '', getattr(upload, 'content_type', '') or '')
    if type_err:
        return Response({'detail': type_err}, status=status.HTTP_400_BAD_REQUEST)
    note = str(request.data.get('note') or '').strip()
    bad_nul = _nul_error('note', note)
    if bad_nul is not None:
        return bad_nul
    # The user-visible original is preserved verbatim (minus NUL, which a Postgres text
    # column cannot store); only the DISK name is sanitised and deduplicated.
    original = (upload.name or '').replace('\x00', '')[:255]
    upload.name = _attachment_disk_name(finding, upload.name or '')
    extra = {'note': note} if _ATTACHMENT_HAS_NOTE else {}
    with transaction.atomic():
        attachment = AuditFindingAttachment.objects.create(
            finding=finding,
            file=upload,
            original_name=original,
            content_type=(getattr(upload, 'content_type', '') or '').split(';')[0].strip().lower()[:120],
            size=upload.size,
            uploaded_by=actor(request),
            **extra,
        )
    return Response(attachment_to_dict(attachment), status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def finding_attachment_delete_view(request, pk: int):
    attachment = AuditFindingAttachment.objects.filter(pk=pk).first()
    if attachment is None:
        return Response({'detail': 'attachment not found'}, status=status.HTTP_404_NOT_FOUND)
    stored_name = attachment.file.name if attachment.file else ''
    storage = attachment.file.storage if attachment.file else default_storage
    with transaction.atomic():
        attachment.delete()
    # Bytes go AFTER the row commit: a dangling file is recoverable garbage, but a live row
    # pointing at deleted bytes would 404 the console viewer. A storage failure is logged,
    # not raised — the row is authoritative and it is already gone.
    if stored_name:
        try:
            storage.delete(stored_name)
        except OSError:
            logger.warning('audit finding attachment %s: file %r could not be removed from storage',
                           pk, stored_name)
    return Response({'deleted': True, 'id': pk})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def findings_bulk_view(request):
    """Apply status/owner/due_date and/or one comment to up to ``BULK_MAX`` findings at once."""
    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)

    bad_ids = Response({'detail': 'ids must be a non-empty list of integer finding ids'},
                       status=status.HTTP_400_BAD_REQUEST)
    raw_ids = data.get('ids')
    if not isinstance(raw_ids, list):
        return bad_ids
    ids, seen = [], set()
    for x in raw_ids:
        if isinstance(x, bool):  # bool is an int subclass; True is not a finding id
            return bad_ids
        if isinstance(x, int):
            v = x
        elif isinstance(x, str):
            try:
                v = int(x.strip())
            except ValueError:
                return bad_ids
        else:
            return bad_ids
        if v not in seen:  # de-duplicate preserving order, so `unknown` mirrors the input
            seen.add(v)
            ids.append(v)
    if not ids:
        return bad_ids
    # The cap applies AFTER de-duplication — 600 copies of one id is one action, not a 400.
    if len(ids) > BULK_MAX:
        return Response({'detail': f'ids is limited to {BULK_MAX} per request'},
                        status=status.HTTP_400_BAD_REQUEST)

    # Key PRESENCE decides, not truthiness — {"owner": ""} is a legitimate "clear the owner".
    patch_input = {k: data[k] for k in ('status', 'owner', 'due_date') if k in data}
    comment = None
    if 'comment' in data:
        comment = str(data['comment'] or '').strip()
        if not comment:
            return Response({'detail': 'comment must not be empty'}, status=status.HTTP_400_BAD_REQUEST)
        bad_nul = _nul_error('comment', comment)
        if bad_nul is not None:
            return bad_nul
    if not patch_input and comment is None:
        return Response({'detail': 'nothing to do (expected status, owner, due_date and/or comment)'},
                        status=status.HTTP_400_BAD_REQUEST)
    fields = {}
    if patch_input:
        fields, err = clean_payload(patch_input, partial=True)
        if err:
            return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)

    # Unknown ids are reported, never fatal: the console may act on a stale snapshot, and
    # failing the whole batch over rows that vanished helps nobody. All-unknown is still a 200.
    known = set(AuditFinding.objects.filter(pk__in=ids).values_list('pk', flat=True))
    unknown = [i for i in ids if i not in known]
    targets = [i for i in ids if i in known]
    who = actor(request)
    updated = commented = 0
    with transaction.atomic():
        if fields:
            # queryset.update() bypasses auto_now, so updated_at is stamped explicitly.
            updated = AuditFinding.objects.filter(pk__in=targets).update(
                **fields, updated_by=who, updated_at=timezone.now())
        if comment is not None:
            AuditFindingComment.objects.bulk_create(
                [AuditFindingComment(finding_id=i, text=comment, author=who) for i in targets]
            )
            commented = len(targets)
    return Response({'updated': updated, 'commented': commented, 'unknown': unknown})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def findings_summary_view(request):
    qs, meta = build_queryset(request.query_params)
    if meta['errors']:
        return Response({'detail': meta['errors'][0]}, status=status.HTTP_400_BAD_REQUEST)
    payload = summary_for(qs, meta['fy'])
    payload['current_fy'] = current_fy()
    payload['fy_options'] = fy_options()
    return Response(payload)


def _render_evidence(evidence) -> str:
    items = evidence if isinstance(evidence, list) else []
    parts = []
    for e in items:
        if isinstance(e, dict):
            parts.append(f"{e.get('type', '')}:{e.get('ref', '')} — {e.get('note', '')}")
    return '; '.join(parts)


def _export_rows(params):
    qs, meta = build_queryset(params)
    if meta['errors']:
        return None, meta['errors'][0]
    qs = qs.annotate(export_comment_count=Count('comments', distinct=True))
    rows = []
    for f in qs:
        rows.append([
            f.ref, f.fy, f.title, f.severity, f.status, f.category,
            f'{f.amount:.2f}' if f.amount is not None else '',
            f.currency, f.owner,
            f.due_date.isoformat() if f.due_date else '',
            f.source, f.check_code, f.asana_gid, f.description,
            _render_evidence(f.evidence),
            f.export_comment_count,
            f.created_by,
            f.created_at.isoformat(),
            f.updated_at.isoformat(),
        ])
    return rows, None


def _export_filename(ext: str) -> str:
    return f'audit-findings-{dt.date.today().isoformat()}.{ext}'


def _csv_response(rows, note: str | None = None) -> HttpResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    writer.writerows(rows)
    resp = HttpResponse(buf.getvalue(), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{_export_filename("csv")}"'
    if note:
        resp['X-Export-Note'] = note
    return resp


@drf_login_required
@require_GET
def findings_export_view(request):
    # Plain Django view (not @api_view): DRF content negotiation treats ?format= as a
    # renderer override and would 404 on csv/xlsx before the view ran. Auth is therefore
    # explicit (@drf_login_required, outermost so an anonymous caller never reaches here)
    # rather than inherited from a permission class.
    fmt = (request.GET.get('format') or 'csv').strip().lower()
    if fmt not in ('csv', 'xlsx'):
        return JsonResponse({'detail': 'format must be csv or xlsx'}, status=status.HTTP_400_BAD_REQUEST)
    rows, err = _export_rows(request.GET)
    if err:
        return JsonResponse({'detail': err}, status=status.HTTP_400_BAD_REQUEST)
    if fmt == 'csv':
        return _csv_response(rows)
    try:
        from openpyxl import Workbook
    except ImportError:
        return _csv_response(rows, note='openpyxl unavailable; degraded to csv')
    wb = Workbook()
    ws = wb.active
    ws.title = 'Findings'
    ws.append(EXPORT_COLUMNS)
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
