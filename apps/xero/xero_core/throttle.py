"""Shared Xero call pacing.

This lived inside document_sync, which meant every other bulk job either
invented its own pacing or had none. The aged-report sweep took the second
route: it slept 1.1s after every 50 calls, which is a ~2,700/min burst against
a 60/min limit, and treated the resulting per-minute 429 as a per-contact error
to skip past. On 28 Aug 2026 one Standard sync spent ~83% of the Klikk tenant's
daily allowance on calls that could not succeed, and still reported success.

Pace every call, wait out a minute-window 429, and stop cleanly when the day's
remaining allowance falls below a floor.
"""
import logging
import time

from xero_python.exceptions import RateLimitException

from apps.xero.xero_core.exceptions import DailyLimitReached
from apps.xero.xero_core.services import MAX_RETRY_AFTER_SLEEP, retry_after_seconds

logger = logging.getLogger(__name__)

# Xero tenant limits: 60 calls/min. Pace just under it.
DEFAULT_CALLS_PER_MINUTE = 55

# Floor of X-DayLimit-Remaining below which discretionary fetch work stops, so
# the nightly pipeline is never starved by an interactive request. MEASURED
# 2026-08-19: the Klikk tenant reads 999 right after the daily reset, i.e. a
# 1,000/day cap (Xero's lower tier), so the floor is 300 (30%).
DEFAULT_HEADROOM = 300


class HeadroomFloorReached(Exception):
    """X-DayLimit-Remaining fell below the configured floor; stop discretionary work."""

    def __init__(self, remaining, floor):
        super().__init__(
            f"Xero X-DayLimit-Remaining={remaining} is below the floor of {floor}; "
            f"stopping to preserve nightly headroom"
        )
        self.remaining = remaining
        self.floor = floor


class RateLimitedCaller:
    """Throttles Xero API calls under the per-minute limit; waits out minute-window 429s,
    raises DailyLimitReached when the daily limit is hit, and HeadroomFloorReached when
    the tenant's remaining daily allowance (from X-DayLimit-Remaining) drops below ``headroom``."""

    def __init__(self, calls_per_minute=DEFAULT_CALLS_PER_MINUTE, api_client=None, headroom=None):
        self.min_interval = 60.0 / calls_per_minute
        self.calls = 0
        self._last_call = 0.0
        self.api_client = api_client      # XeroApiClient (exposes day_limit_remaining)
        self.headroom = headroom

    @property
    def day_limit_remaining(self):
        return getattr(self.api_client, 'day_limit_remaining', None)

    def check_headroom(self):
        remaining = self.day_limit_remaining
        if self.headroom is not None and remaining is not None and remaining < self.headroom:
            raise HeadroomFloorReached(remaining, self.headroom)

    def __call__(self, fn, *args, **kwargs):
        self.check_headroom()
        for attempt in (1, 2):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                return fn(*args, **kwargs)
            except RateLimitException as e:
                # Day-window 429s are normally converted to DailyLimitReached by
                # XeroApiClient's HTTP guard before reaching here; this branch
                # handles minute-window 429s, with the same cap as a backstop.
                problem = (e.rate_limit or '').lower()
                delay = retry_after_seconds(e)
                if problem == 'day' or delay > MAX_RETRY_AFTER_SLEEP or attempt == 2:
                    raise DailyLimitReached(
                        f"Xero rate limit ({problem or 'unknown window'}, "
                        f"Retry-After={delay}s) — aborting instead of sleeping",
                        retry_after=delay,
                    ) from e
                logger.warning("Xero per-minute rate limit hit; sleeping %ss before retry", delay)
                time.sleep(delay)
