"""
AuditorGateMiddleware — the hard server-side gate for auditor accounts.

External auditors get User.role == 'auditor'. Such accounts may ONLY make
safe-method (GET/HEAD/OPTIONS) requests to the audit surface (/audit/...),
plus the auth endpoints they need to hold a session. Everything else — the
Xero GL, Investec bank data, dashboards, pricelist, MCP surfaces, Django
admin, and every write anywhere — answers 403.

This is deliberately middleware rather than a DRF permission class: most
views set their own ``permission_classes``, which would silently override a
DEFAULT_PERMISSION_CLASSES entry, and several file/export endpoints are
plain Django views that never consult DRF permissions at all. Middleware
runs before every view regardless.

DRF authenticates at the view layer, so ``request.user`` is anonymous here
for JWT requests. The middleware therefore resolves the caller itself:

  - a valid simplejwt Bearer token   -> that user's role is enforced
  - a session-authenticated user     -> enforced (covers the Django admin)
  - anything else (anonymous, the opaque MCP service token, a garbage or
    expired JWT) -> passed through untouched; the view's own
    authentication/permission stack remains responsible for those.

Fail-open for non-auditors by design: this gate only ever SUBTRACTS access
from auditor accounts; it grants nothing.
"""

import re

from django.http import JsonResponse
from rest_framework_simplejwt.authentication import JWTAuthentication

SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

# Read-only surface an auditor may touch with safe methods.
AUDITOR_READ_PREFIXES = (
    '/audit/',            # receipts register, findings, slips, attachment/file views, exports
)

# Auth endpoints an auditor may hit with ANY method (login/refresh/verify are
# POSTs, and the axios client attaches the Bearer token to them too).
AUDITOR_AUTH_RE = re.compile(r'^/api/auth/(login|refresh|token(/(refresh|verify))?)/$')


class AuditorGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._jwt_auth = JWTAuthentication()

    def __call__(self, request):
        user = self._resolve_user(request)
        if user is not None and getattr(user, 'role', None) == 'auditor':
            if not self._allowed(request):
                return JsonResponse(
                    {'detail': 'Auditor accounts have read-only access to the audit pages.'},
                    status=403,
                )
        return self.get_response(request)

    def _resolve_user(self, request):
        # Session auth (Django admin, browsable API) — set by AuthenticationMiddleware.
        session_user = getattr(request, 'user', None)
        if session_user is not None and session_user.is_authenticated:
            return session_user
        # JWT — resolved here because DRF only authenticates inside the view.
        header = request.META.get('HTTP_AUTHORIZATION', '')
        if not header.startswith('Bearer '):
            return None
        try:
            result = self._jwt_auth.authenticate(request)
        except Exception:
            # Opaque service token / expired / malformed JWT: not this gate's
            # problem — the view's own auth stack rejects or accepts it.
            return None
        return result[0] if result else None

    def _allowed(self, request):
        # CORS preflight must never be blocked (and discloses nothing).
        if request.method == 'OPTIONS':
            return True
        path = request.path
        if AUDITOR_AUTH_RE.match(path):
            return True
        if request.method in SAFE_METHODS:
            return any(path.startswith(p) for p in AUDITOR_READ_PREFIXES)
        return False
