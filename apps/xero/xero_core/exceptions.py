"""
Custom exceptions for xero_core app.
"""


class XeroCoreException(Exception):
    """Base exception for xero_core app."""
    pass


class DailyLimitReached(XeroCoreException):
    """Xero's per-day API limit for the tenant is exhausted.

    Retrying (or sleeping out the multi-hour Retry-After) cannot help until the
    daily counter resets, so callers must abort the run cleanly and let the
    next scheduled run pick up after the reset.
    """

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        # Seconds until the daily counter resets, from Xero's Retry-After header (may be None).
        self.retry_after = retry_after


class TenantReauthRequired(XeroCoreException):
    """The tenant's Xero refresh token is dead — only a human re-authorization fixes it.

    Xero returns 400 invalid_grant once a refresh token is revoked/expired/rotated
    away. Retrying burns the daily API budget and floods the logs forever (the
    hourly out-of-sync job retried two dead tenants 59x overnight, 2026-08-19),
    because nothing the app does can mint a new token. Callers must SKIP the
    tenant until someone re-runs the OAuth consent flow in the console.
    """

    def __init__(self, message, tenant_id=None):
        super().__init__(message)
        self.tenant_id = tenant_id

# Add custom exceptions here

