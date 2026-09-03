"""
AuditorGateMiddleware — the hard server-side gate for auditor accounts, and
the enforcement point for the forced-password-change flag.

TWO gates live here, checked in that order:

  A. ``must_change_password`` (ANY role). An account handed a temporary
     password may reach the auth endpoints and POST /api/auth/change-password/
     and NOTHING else, until it sets its own password. Everything else answers
     403 with ``code: "password_change_required"`` so the console can route to
     the change-password screen even from a stale tab. This gate applies to
     standard users too — a temporary credential is a temporary credential.

  B. the auditor read-only gate, described below.

External auditors get User.role == 'auditor'. Such accounts may ONLY make
safe-method (GET/HEAD/OPTIONS) requests to the audit surface (/audit/...),
plus the auth endpoints they need to hold a session, plus ONE narrow write
exception: POSTing a comment on a finding or on a receipt. Everything else —
the Xero GL, Investec bank data, dashboards, pricelist, MCP surfaces, Django
admin, and every other write anywhere — answers 403.

The comment exception exists because an auditor who cannot ask a question on
the record is an auditor who asks it by email, off the audit trail. It is
deliberately the narrowest possible hole: exactly POST, on exactly the two
comment collection routes (AUDITOR_WRITE_RE), and nothing else. Comments are
append-only for everyone, so this grants no edit or delete of anything —
including the auditor's own comments. Bulk endpoints, review/decision
writes, attachment uploads, link writes, and every PATCH/DELETE stay 403.
Authorship is stamped by the views from request.user, so an auditor's
comments carry their own username (their email address) — the gate never
touches attribution.

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

# The one path a must-change-password account may POST to. Auditors may reach it
# too (both before AND after the flag clears — voluntary changes are allowed).
CHANGE_PASSWORD_PATH = '/api/auth/change-password/'

# The ONLY write an auditor may make: POST a comment on a finding or a receipt.
# Anchored at both ends and method-checked against POST alone, so it cannot be
# widened by a trailing segment (…/comments/1/) or a different verb. The receipt
# key is a sha256 — alphanumeric by construction; the class deliberately excludes
# '/', '.' and every other separator so no sibling route can be reached through it.
AUDITOR_WRITE_RE = re.compile(
    r'^/audit/(findings/\d+/comments|receipts/[A-Za-z0-9]+/comments)/$'
)


def resolve_request_user(request, jwt_auth=None):
    """The caller behind this request, or None.

    Session auth first (Django admin, browsable API), then a simplejwt Bearer
    token — DRF only authenticates inside the view, so anything running at
    middleware level has to do this itself. Shared so other call sites (the
    activity trail, signed file views) identify the caller the SAME way the
    gate does; two different answers to "who is this" is how gates get bypassed.

    Returns None for anonymous callers, the opaque MCP service token, and
    expired/garbage JWTs — the view's own auth stack owns those.
    """
    session_user = getattr(request, 'user', None)
    if session_user is not None and session_user.is_authenticated:
        return session_user
    header = request.META.get('HTTP_AUTHORIZATION', '')
    if not header.startswith('Bearer '):
        return None
    try:
        result = (jwt_auth or JWTAuthentication()).authenticate(request)
    except Exception:
        return None
    return result[0] if result else None


class AuditorGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._jwt_auth = JWTAuthentication()

    def __call__(self, request):
        user = self._resolve_user(request)
        if user is None:
            return self.get_response(request)
        # Gate A first: a temporary credential is held here regardless of role,
        # so an auditor who has not changed their password cannot even read.
        if getattr(user, 'must_change_password', False) and not self._password_change_allowed(request):
            return JsonResponse(
                {'detail': 'Password change required.', 'code': 'password_change_required'},
                status=403,
            )
        if getattr(user, 'role', None) == 'auditor' and not self._allowed(request):
            return JsonResponse(
                {'detail': 'Auditor accounts have read-only access to the audit pages.'},
                status=403,
            )
        return self.get_response(request)

    def _resolve_user(self, request):
        return resolve_request_user(request, self._jwt_auth)

    def _password_change_allowed(self, request):
        """What an account holding a temporary password may still do."""
        if request.method == 'OPTIONS':  # CORS preflight; discloses nothing
            return True
        path = request.path
        if path == CHANGE_PASSWORD_PATH:
            return True
        # Login/refresh must stay reachable or the client cannot even hold the
        # session it needs to POST the new password.
        return bool(AUDITOR_AUTH_RE.match(path))

    def _allowed(self, request):
        # CORS preflight must never be blocked (and discloses nothing).
        if request.method == 'OPTIONS':
            return True
        path = request.path
        if AUDITOR_AUTH_RE.match(path) or path == CHANGE_PASSWORD_PATH:
            return True
        if request.method in SAFE_METHODS:
            return any(path.startswith(p) for p in AUDITOR_READ_PREFIXES)
        # Narrow write exception: POST a comment, nothing else, nowhere else.
        if request.method == 'POST' and AUDITOR_WRITE_RE.match(path):
            return True
        return False
