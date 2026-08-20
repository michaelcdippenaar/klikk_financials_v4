"""Resolving the active Xero credentials for a request.

Why this module exists
----------------------
Before the 2026-08-20 lockdown (SECURITY-NOTE.md) the Xero views were
``AllowAny``, so most real traffic arrived anonymous and took the *safe*
branch of this shape::

    if request.user.is_authenticated:
        credentials = XeroClientCredentials.objects.get(user=request.user, active=True)
    else:
        credentials = XeroClientCredentials.objects.filter(active=True).first()
        if not credentials:
            return Response(..., status=403)

The lockdown made every caller authenticated, which pushed real users down
the ``.get()`` branch for the first time. ``.get()`` raises ``DoesNotExist``
for any logged-in user who does not personally own an active credentials row
-- an unhandled exception, so DRF returns **500** where it should return a
clean 4xx. That is what ``GET /xero/core/tenants/`` was doing to the console.

The fix is not to special-case that one view: the same shape appears in
xero_core, xero_metadata and xero_sync, so it lives here once.

A note on the fallback, which is a deliberate decision and not an accident
-------------------------------------------------------------------------
When the authenticated user has no credentials row of their own we fall back
to the first active row in the table, exactly as the old anonymous branch
did. This is a single-operator deployment: every account belongs to MC, and
without the fallback the console would 403 the moment it authenticated as an
account other than the credentials owner.

It does mean an authenticated user can act through another user's Xero
credentials. That is NOT a widening of what the lockdown closed -- before the
lockdown *any anonymous caller on the internet* got this same fallback -- but
if this system ever grows a user who is not MC, this fallback must become a
per-user lookup with no cross-user borrowing. Flagged for MC.
"""

from apps.xero.xero_auth.models import XeroClientCredentials


def resolve_active_credentials(request):
    """Return the active XeroClientCredentials for ``request``, or ``None``.

    Never raises. ``None`` means "no active Xero credentials exist at all",
    which callers should turn into a clean 403 rather than a 500.

    ``.filter().first()`` rather than ``.get()`` on purpose: it cannot raise
    ``DoesNotExist``, and it cannot raise ``MultipleObjectsReturned`` either
    if the table ever ends up with two active rows for one user.
    """
    user = getattr(request, "user", None)
    # `getattr(user, "pk", None) is not None` is load-bearing, not defensive
    # noise. A caller authenticated by the shared service token gets a
    # klikk_business_intelligence.permissions.ServiceAccount on request.user:
    # is_authenticated is True but it is NOT a Django model and pk is None, so
    # passing it to .filter(user=...) raises and turns the MCP server's calls
    # into 500s. A machine caller has no credentials row of its own by
    # definition, so it goes straight to the shared fallback below.
    if user is not None and getattr(user, "is_authenticated", False) \
            and getattr(user, "pk", None) is not None:
        owned = (
            XeroClientCredentials.objects
            .filter(user=user, active=True)
            .order_by("pk")
            .first()
        )
        if owned is not None:
            return owned
    # See the module docstring: deliberate shared-operator fallback.
    return (
        XeroClientCredentials.objects
        .filter(active=True)
        .order_by("pk")
        .first()
    )


def resolve_active_credentials_user(request):
    """The user to act as when calling Xero, or ``None`` if none can be resolved."""
    credentials = resolve_active_credentials(request)
    return credentials.user if credentials is not None else None
