"""Signed, read-only viewer for WhatsApp Slippies receipts stored in whatsapp.klikk_slips.

URL: /audit/slip/<sha256>/?s=<sig>   where sig = HMAC-SHA256(SECRET_KEY, sha256)[:32]
Links are unguessable (the backend is otherwise public-read) and never expire so spreadsheet
links keep working. Bytes come straight from Postgres; nothing is written.
"""
import hashlib
import hmac
import mimetypes

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.views.decorators.http import require_GET


def slip_signature(sha256: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), sha256.encode(), hashlib.sha256).hexdigest()[:32]


def slip_url(sha256: str, base: str | None = None) -> str:
    base = base or getattr(settings, "SLIP_VIEW_BASE_URL", "https://console.8-bit.space/backend")
    return f"{base}/audit/slip/{sha256}/?s={slip_signature(sha256)}"


@require_GET
def slip_file_view(request, sha256: str):
    sig = request.GET.get("s", "")
    if not sig or not hmac.compare_digest(sig, slip_signature(sha256)):
        return HttpResponseForbidden("invalid signature")
    with connection.cursor() as cur:
        cur.execute(
            "select filename, mime_ext, file_bytes from whatsapp.klikk_slips where sha256 = %s",
            [sha256],
        )
        row = cur.fetchone()
    if not row or row[2] is None:
        return HttpResponseNotFound("slip not found")
    filename, mime_ext, data = row
    ctype = mimetypes.guess_type(f"x.{mime_ext or ''}")[0] or "application/octet-stream"
    resp = HttpResponse(bytes(data), content_type=ctype)
    resp["Content-Disposition"] = f'inline; filename="{filename or sha256}.{mime_ext or "bin"}"'
    resp["Cache-Control"] = "private, max-age=3600"
    return resp
