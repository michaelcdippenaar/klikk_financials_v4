from django.db import DatabaseError
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class BrowserAuthenticationUnavailable(Exception):
    """The user store could not be reached while validating a browser JWT."""


def authenticate_browser_request(request):
    """Authenticate only the existing SimpleJWT browser access token.

    The project-wide DRF stack also accepts service and legacy tokens. Those
    credentials are intentionally excluded from the web GraphQL boundary.
    """
    try:
        authenticated = JWTAuthentication().authenticate(request)
    except AuthenticationFailed:
        authenticated = None
    except DatabaseError as exc:
        raise BrowserAuthenticationUnavailable from exc
    if authenticated is None:
        return False
    request.user, request.auth = authenticated
    return True
