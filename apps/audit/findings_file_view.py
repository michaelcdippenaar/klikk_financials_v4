"""Signed, read-only viewer for audit-finding attachments (``audit_auditfindingattachment``).

URL: /audit/findings/attachments/<pk>/file/?s=<sig>   (plural — moved from .../attachment/<pk>/file/)
     where sig = HMAC-SHA256(SECRET_KEY, f'auditfinding:{pk}')[:32]  (findings_services.attachment_signature)
Links are unguessable and never expire so exported spreadsheets and pasted links keep
working. Bytes come straight from the FileField's storage; nothing is written.

This view is DELIBERATELY unauthenticated and must stay that way: the console's finding
modal loads it in a plain <img>/<iframe> and exported spreadsheets link to it, neither of
which can carry a Bearer token. The HMAC in ?s= is its only guard, which is why every
endpoint that hands out these links (findings_views — list, detail, attachment upload) all
require authentication — same pattern and same reasoning as apps/audit/slip_view.py.

Hardening (CONTRACT-2 §1):
- the signature is verified BEFORE any DB lookup, so an invalid caller learns nothing
  about which attachment ids exist;
- every response carries ``X-Content-Type-Options: nosniff``;
- the served content type comes from the upload allowlist
  (findings_views.ATTACHMENT_ALLOWED_TYPES) — a stored type outside the allowlist is never
  echoed back to the browser; anything unrecognised is forced to application/octet-stream
  with ``Content-Disposition: attachment`` so it downloads instead of rendering inline.
"""
import hmac
import os

from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.views.decorators.http import require_GET

from .findings_services import attachment_signature
from .findings_views import ATTACHMENT_ALLOWED_TYPES
from .models import AuditFindingAttachment

_ALLOWED_CONTENT_TYPES = {t for types in ATTACHMENT_ALLOWED_TYPES.values() for t in types}


def _served_content_type(attachment):
    """The allowlisted content type to serve, or None when nothing trustworthy matches.

    The stored content_type was validated on upload, but rows can predate the allowlist
    (or the allowlist can shrink), so it is re-checked on the way OUT too — the client-
    supplied type is never served unless it is on the allowlist right now. Fallback is
    the canonical type for the on-disk extension, which the uploader sanitised.
    """
    stored = (attachment.content_type or '').split(';')[0].strip().lower()
    if stored in _ALLOWED_CONTENT_TYPES:
        return stored
    ext = os.path.splitext(attachment.file.name or '')[1].lstrip('.').lower()
    allowed = ATTACHMENT_ALLOWED_TYPES.get(ext)
    return allowed[0] if allowed else None


def _header_filename(attachment, pk: int) -> str:
    """ASCII-only, quote/control-free filename for Content-Disposition (no header injection)."""
    raw = attachment.original_name or f'attachment-{pk}'
    clean = ''.join(ch for ch in raw if 32 <= ord(ch) < 127 and ch not in '"\\')
    return clean.strip() or f'attachment-{pk}'


@require_GET
def finding_attachment_file_view(request, pk: int):
    # Signature check BEFORE any DB lookup so an invalid caller learns nothing
    # about which attachment ids exist (same as document_views.xero_document_file_view).
    sig = request.GET.get('s', '')
    if not sig or not hmac.compare_digest(sig, attachment_signature(pk)):
        return HttpResponseForbidden('invalid signature')
    attachment = AuditFindingAttachment.objects.filter(pk=pk).first()
    if attachment is None or not attachment.file or not attachment.file.name:
        return HttpResponseNotFound('attachment not found')
    try:
        with attachment.file.open('rb') as fh:
            data = fh.read()
    except (FileNotFoundError, OSError, ValueError):
        return HttpResponseNotFound('attachment not found')
    ctype = _served_content_type(attachment)
    filename = _header_filename(attachment, pk)
    if ctype is None:
        # Outside the allowlist: never let the browser render it — force a download
        # under the one content type nothing executes as.
        resp = HttpResponse(data, content_type='application/octet-stream')
        resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    else:
        resp = HttpResponse(data, content_type=ctype)
        resp['Content-Disposition'] = f'inline; filename="{filename}"'
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Cache-Control'] = 'private, max-age=3600'
    return resp
