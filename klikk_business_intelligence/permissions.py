"""Shared-secret authentication for machine callers that have no Django user.

Why an authentication class and not just a permission class
-----------------------------------------------------------
``REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']`` starts with simplejwt's
``JWTAuthentication``. DRF runs *every* authentication class before it runs a *single*
permission class — and ``JWTAuthentication`` does not politely decline a token it cannot
parse. Given ``Authorization: Bearer <opaque-service-token>`` it raises ``InvalidToken``
and the request is finished with a 401 before ``has_permission()`` is ever called. So a
permission class on its own can never accept a shared service token: the token has to be
consumed by an authentication class that runs *first*, which is why
``ServiceTokenAuthentication`` is prepended to that list in ``settings/base.py``.

The pair below is deliberately narrow:

* ``ServiceTokenAuthentication`` returns ``None`` — never an exception — for every
  request that is not carrying exactly the configured token, so ``JWTAuthentication``
  still gets its turn and every other endpoint in the project behaves exactly as it did
  before this module existed.
* ``HasServiceToken`` requires an authenticated caller for every method. Until 2026-08-20
  reads passed through untouched because the project default was ``AllowAny``; the default
  is now ``IsAuthenticated`` (SECURITY-NOTE.md lockdown) and this class must not be the one
  loophole that keeps pricelist reads anonymous.

Applied to the three ``apps/pricelist`` write actions. See ``docs/pricelist.md`` §4.
"""
import logging

from django.conf import settings
from django.utils.crypto import constant_time_compare
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.permissions import SAFE_METHODS, BasePermission

logger = logging.getLogger(__name__)

# Log-once guard: the "you have not configured a token" warning is useful exactly once per
# process. Without the guard it would fire on every write attempt and drown the log.
_warned_missing_token = False


def _configured_token() -> str:
    """The configured shared secret, or '' when unset. Never logged, never returned to a client."""
    return (getattr(settings, 'KLIKK_API_TOKEN', '') or '').strip()


def _warn_missing_token_once() -> None:
    """Warn once per process that KLIKK_API_TOKEN is unset. Never logs the token value."""
    global _warned_missing_token
    if _warned_missing_token:
        return
    _warned_missing_token = True
    logger.warning(
        'KLIKK_API_TOKEN is not set, so service-token writes are denied: only a caller '
        'with an authenticated session (the console JWT) can write to endpoints guarded '
        'by HasServiceToken. Set KLIKK_API_TOKEN on the Django container and on the MCP '
        'container to let machine callers write.'
    )


class ServiceAccount:
    """Non-persisted stand-in user for a caller authenticated by the shared service token.

    Not a Django user and never saved; it exists only so DRF has something to put on
    ``request.user`` with ``is_authenticated`` True.
    """

    is_authenticated = True
    is_active = True
    is_anonymous = False
    is_staff = False
    is_superuser = False
    username = 'service-token'
    pk = None

    def __str__(self) -> str:
        return 'service-token'


class ServiceTokenAuthentication(BaseAuthentication):
    """Authenticate ``Authorization: Bearer <settings.KLIKK_API_TOKEN>``.

    Returns None (NOT an exception) when the header is absent, is not a Bearer
    header, or does not match — so JWTAuthentication still gets its turn and the
    console's JWT keeps working. Comparison is constant-time.
    """

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if len(parts) != 2 or parts[0].lower() != b'bearer':
            return None
        configured = _configured_token()
        if not configured:
            # Nothing to match against. Stay silent here and let HasServiceToken do the
            # warning, so a plain GET on an unrelated endpoint never trips it.
            return None
        try:
            token = parts[1].decode('utf-8')
        except UnicodeDecodeError:
            return None
        if not constant_time_compare(token, configured):
            # Not our token — very probably a JWT. Decline so JWTAuthentication can try.
            return None
        return (ServiceAccount(), token)

    def authenticate_header(self, request) -> str:
        """Return the SAME WWW-Authenticate value JWTAuthentication returns: 'Bearer realm="api"'.

        This is not decoration, it is what keeps the change behaviour-neutral. DRF picks the
        401-vs-403 shape from ``get_authenticators()[0].authenticate_header()`` ONLY
        (rest_framework/views.py: get_authenticate_header -> handle_exception), and coerces
        NotAuthenticated to 403 when that returns None. Because this class is now first on
        DEFAULT_AUTHENTICATION_CLASSES, omitting the method would silently turn every
        unauthenticated 401 in the project — /xero/cube/*, every IsAuthenticated view — into a
        403, and would make an unauthenticated pricelist write 403 rather than the documented
        401. Returning simplejwt's exact string preserves today's behaviour byte for byte.
        """
        return 'Bearer realm="api"'


class HasServiceToken(BasePermission):
    """Access requires the shared service token or a logged-in user.

    Since the 2026-08-20 lockdown reads are gated too: an explicit
    ``permission_classes`` REPLACES the project default rather than adding to it,
    so if this class kept its old pass-through for SAFE_METHODS the pricelist
    reads would remain the only anonymous data endpoints in the project.
    """

    message = ('This endpoint requires Authorization: Bearer <KLIKK_API_TOKEN> '
               'or an authenticated session.')

    def has_permission(self, request, view) -> bool:
        if request.method not in SAFE_METHODS and not _configured_token():
            _warn_missing_token_once()
        user = getattr(request, 'user', None)
        if isinstance(user, ServiceAccount):
            return True
        if user is not None and getattr(user, 'is_authenticated', False):
            # The console, which sends a JWT via klikk_portal/src/api/client.js. Blocking it
            # would break editing on the Price List page.
            return True
        return False
