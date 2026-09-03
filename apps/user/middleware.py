"""
AuditorGateMiddleware — the hard server-side gate for the two RESTRICTED
roles (auditor, service_readonly), and the enforcement point for the
forced-password-change flag.

THREE gates live here, checked in that order:

  A. ``must_change_password`` (ANY role). An account handed a temporary
     password may reach the auth endpoints and POST /api/auth/change-password/
     and NOTHING else, until it sets its own password. Everything else answers
     403 with ``code: "password_change_required"`` so the console can route to
     the change-password screen even from a stale tab. This gate applies to
     standard users too — a temporary credential is a temporary credential.

  B. the auditor read-only gate, described below.

  C. the service_readonly gate — the machine equivalent of B, for a
     non-human identity whose credential lives on someone's laptop.

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

The service_readonly role (gate C) exists for the Excel add-in identity. That
add-in is read-only in its JavaScript, but until 2026-09-03 its CREDENTIAL was
not: the `excel-addin` user was role=standard, so its DRF authtoken passed
every IsAuthenticated view in the project. That included
XeroCreateDraftInvoiceView — the one path in this codebase that WRITES to Xero
— and the seven Xero sync triggers, any of which can burn the 5,000-call daily
API budget. The token sits in a laptop's localStorage and is posted to whatever
baseUrl is typed into the pane's settings box, so "the client happens not to
call those endpoints" was the only thing standing between a copied token and a
Xero draft.

So the role is narrowed to exactly the surface the add-in uses: safe methods on
the journal cube (/xero/data/journals/...), the HMAC-signed document file route
the drill's receipt links point at, and the cube COLLABORATION writes — posting
and status-flipping a cube comment, saving and deleting a subset or a view.
Those writes land in our own Postgres (app.cube_comments and friends) and reach
nothing upstream. Every Xero write, every sync trigger, every other app in the
project, and the Django admin answer 403.

Note gate C is a real narrowing and not theatre only because resolve_request_user
below understands `Authorization: Token <key>`. It originally resolved sessions
and Bearer JWTs alone, which is precisely the credential shape the add-in does
NOT use — a DRF authtoken would have sailed past an otherwise-correct gate.

This is deliberately middleware rather than a DRF permission class: most
views set their own ``permission_classes``, which would silently override a
DEFAULT_PERMISSION_CLASSES entry, and several file/export endpoints are
plain Django views that never consult DRF permissions at all. Middleware
runs before every view regardless.

DRF authenticates at the view layer, so ``request.user`` is anonymous here
for token requests. The middleware therefore resolves the caller itself:

  - a valid simplejwt Bearer token   -> that user's role is enforced
  - a valid DRF authtoken (``Token``) -> that user's role is enforced
  - a session-authenticated user     -> enforced (covers the Django admin)
  - anything else (anonymous, the opaque MCP service token, a garbage or
    expired credential) -> passed through untouched; the view's own
    authentication/permission stack remains responsible for those.

Fail-open for unrestricted roles by design: this gate only ever SUBTRACTS
access from auditor and service_readonly accounts; it grants nothing. A
``standard`` user, the MCP service token and an anonymous caller all see
exactly the behaviour they saw before this module existed.
"""

import re

from django.http import JsonResponse
from rest_framework.authentication import TokenAuthentication
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

# The ONLY writes an auditor may make: POST a comment on a finding or a receipt,
# or POST a REPLY on a comment in the cube-comment register.
# Anchored at both ends and method-checked against POST alone, so it cannot be
# widened by a trailing segment (…/comments/1/) or a different verb. The receipt
# key is a sha256 — alphanumeric by construction; the class deliberately excludes
# '/', '.' and every other separator so no sibling route can be reached through it.
#
# The cube alternative is the same shape and the same reasoning: a numeric comment
# id and the literal 'replies', with nothing after it. It grants POST on
# /audit/cube-comments/<id>/replies/ ONLY — not the register list (a safe method,
# already allowed by the /audit/ prefix), not a reply-to-a-reply URL, and nothing
# under /xero/data/, where the register itself lives and the whole GL sits.
AUDITOR_WRITE_RE = re.compile(
    r'^/audit/(findings/\d+/comments|receipts/[A-Za-z0-9]+/comments'
    r'|cube-comments/\d+/replies)/$'
)


# ── service_readonly: the Excel add-in identity ─────────────────────────────
#
# Everything below is an ALLOWLIST. A path that matches none of these is 403,
# which is the whole point: the failure mode of a denylist is that the next
# Xero write someone adds is reachable by default.

# Safe methods only, and only the journal cube. This is the surface the pane
# reads: journals/search/, journals/filters/, journals/pivot/ and its
# dimensions/, members/, drill/, subsets/, views/, comments/ children.
# Deliberately NOT '/xero/data/' — that prefix also carries invoices/,
# quotes/, aged-*/ and the sync triggers.
SERVICE_READONLY_READ_PREFIXES = (
    '/xero/data/journals/',
)

# The receipt links a drill puts in its rows. Already HMAC-signed per document
# id and readable with no session at all (it is a plain Django view, not DRF),
# so allowing it grants nothing the signature does not already grant — but the
# pane may fetch it with the Token header attached, and a gate that 403s a link
# it just minted is a gate that gets widened in a hurry by whoever debugs it.
SERVICE_READONLY_DOC_RE = re.compile(r'^/xero/data/documents/\d+/file/$')

# The cube COLLABORATION writes. All four land in our own Postgres and reach
# nothing upstream: a comment on a cell, that comment's status, a saved member
# subset, a saved view. Anchored at both ends and method-checked, so no sibling
# route is reachable by appending a segment.
SERVICE_READONLY_POST_RE = re.compile(
    r'^/xero/data/journals/pivot/'
    r'(comments|comments/bulk|comments/\d+/status|subsets|views)/$'
)

# Saved subsets and views are the only things this identity may remove, and
# only by name/dimension in the query string — there is no DELETE on comments
# (they are append-only/retract-by-empty, same as everywhere else in this app).
SERVICE_READONLY_DELETE_RE = re.compile(
    r'^/xero/data/journals/pivot/(subsets|views)/$'
)


def resolve_request_user(request, jwt_auth=None, token_auth=None):
    """The caller behind this request, or None.

    Session auth first (Django admin, browsable API), then a simplejwt Bearer
    token, then a DRF authtoken (``Authorization: Token <key>``) — DRF only
    authenticates inside the view, so anything running at middleware level has
    to do this itself. Shared so other call sites (the activity trail, signed
    file views) identify the caller the SAME way the gate does; two different
    answers to "who is this" is how gates get bypassed.

    The DRF-authtoken branch is what makes the service_readonly gate real: that
    is the credential shape the Excel add-in uses, and while this function
    understood only sessions and Bearer tokens, a role gate on such an account
    would have matched nothing and silently granted everything.

    Returns None for anonymous callers, the opaque MCP service token (a Bearer
    header that resolves to the non-persisted ServiceAccount, which carries no
    role), and expired/garbage credentials — the view's own auth stack owns
    those.
    """
    session_user = getattr(request, 'user', None)
    if session_user is not None and session_user.is_authenticated:
        return session_user
    header = request.META.get('HTTP_AUTHORIZATION', '')
    if header.startswith('Bearer '):
        try:
            result = (jwt_auth or JWTAuthentication()).authenticate(request)
        except Exception:
            return None
        return result[0] if result else None
    if header.startswith('Token '):
        try:
            result = (token_auth or TokenAuthentication()).authenticate(request)
        except Exception:
            return None
        return result[0] if result else None
    return None


class AuditorGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._jwt_auth = JWTAuthentication()
        self._token_auth = TokenAuthentication()

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
        role = getattr(user, 'role', None)
        if role == 'auditor' and not self._allowed(request):
            return JsonResponse(
                {'detail': 'Auditor accounts have read-only access to the audit pages.'},
                status=403,
            )
        if role == 'service_readonly' and not self._service_readonly_allowed(request):
            return JsonResponse(
                {'detail': 'This service account is read-only on the journal cube.'},
                status=403,
            )
        return self.get_response(request)

    def _resolve_user(self, request):
        return resolve_request_user(request, self._jwt_auth, self._token_auth)

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

    def _service_readonly_allowed(self, request):
        """Gate C. Pure allowlist — see the constants above.

        Everything this does NOT return True for answers 403, including
        /xero/data/invoices/create-draft/ (the only Xero write in the project)
        and all seven sync triggers, which is the reason the role exists.
        """
        # CORS preflight must never be blocked (and discloses nothing).
        if request.method == 'OPTIONS':
            return True
        path = request.path
        if request.method in SAFE_METHODS:
            return (
                any(path.startswith(p) for p in SERVICE_READONLY_READ_PREFIXES)
                or bool(SERVICE_READONLY_DOC_RE.match(path))
            )
        if request.method == 'POST':
            return bool(SERVICE_READONLY_POST_RE.match(path))
        if request.method == 'DELETE':
            return bool(SERVICE_READONLY_DELETE_RE.match(path))
        return False
