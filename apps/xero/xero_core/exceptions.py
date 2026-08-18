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

# Add custom exceptions here

