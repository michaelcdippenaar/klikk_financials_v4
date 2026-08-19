from django.db import models


class XeroTenant(models.Model):
    tenant_id = models.CharField(max_length=100, unique=True, primary_key=True)
    tenant_name = models.CharField(max_length=100)
    tracking_category_1_id = models.CharField(
        max_length=64, blank=True, null=True,
        help_text='Xero TrackingCategoryID for slot 1 (first category from API). Stable even if name changes.'
    )
    tracking_category_2_id = models.CharField(
        max_length=64, blank=True, null=True,
        help_text='Xero TrackingCategoryID for slot 2 (second category from API).'
    )
    fiscal_year_start_month = models.IntegerField(
        null=True, blank=True,
        help_text='Month when fiscal year starts (1-12). Fetched from Xero Organisation. Default 7 (July) if not set.'
    )
    # --- Re-authorization state -------------------------------------------
    # Set when Xero rejects the refresh token (400 invalid_grant). Nothing the
    # app can do fixes that, so every scheduled/bulk caller must SKIP the
    # tenant until a human re-runs the OAuth consent flow (which clears it).
    reauth_required = models.BooleanField(
        default=False,
        help_text='Xero refresh token is dead; skip all scheduled syncs until re-authorized in the console.'
    )
    reauth_reason = models.TextField(
        blank=True, default='',
        help_text='Why re-authorization is needed (last token-refresh error from Xero).'
    )
    reauth_flagged_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When reauth_required was first set (cleared on successful re-authorization).'
    )

    def get_fiscal_year_start_month(self):
        """Return fiscal year start month (1-12). Uses Xero value if set, else default 7."""
        if self.fiscal_year_start_month is not None and 1 <= self.fiscal_year_start_month <= 12:
            return self.fiscal_year_start_month
        from apps.xero.xero_metadata.utils import DEFAULT_FISCAL_YEAR_START_MONTH
        return DEFAULT_FISCAL_YEAR_START_MONTH

    def __str__(self):
        return self.tenant_name

    def get_tracking_slot(self, tracking_category_id):
        """Return 1 or 2 based on TrackingCategoryID; None if no match."""
        if not tracking_category_id:
            return None
        if tracking_category_id == self.tracking_category_1_id:
            return 1
        if tracking_category_id == self.tracking_category_2_id:
            return 2
        return None


class XeroApiQuota(models.Model):
    """Last-seen per-tenant API allowance, straight from Xero's own headers.

    Every Xero response carries X-DayLimit-Remaining / X-MinLimit-Remaining.
    That is the ONLY trustworthy budget signal: the daily window resets at an
    unpublished per-tenant instant and the cap is per-tenant (Klikk's is
    1,000/day, not the documented 5,000), so any locally computed count
    (XeroApiCallLog) under-reports — probes, backfills and one-shot scripts
    never log, and the portal ended up showing 0 / 5,000 while Xero had
    already counted dozens of calls. XeroApiClient._record_limit_headers()
    persists what it sees here so the portal can display Xero's number.

    Best-effort telemetry: writes are throttled per client instance (see
    QUOTA_PERSIST_INTERVAL_SECONDS in services.py) and never raise, so the
    row may lag a few seconds behind reality and may be missing entirely for a
    tenant that has not made a call since this table was added. Consumers must
    treat an absent row as unknown, not zero.

    tenant_id is deliberately NOT a FK to XeroTenant: the guard must be able
    to record headers even while a tenant row is missing or being created, and
    it must never fail on an FK constraint mid-API-call.
    """
    tenant_id = models.CharField(max_length=100, primary_key=True)
    day_remaining = models.IntegerField(
        null=True, blank=True,
        help_text='X-DayLimit-Remaining from the most recently recorded Xero response.'
    )
    min_remaining = models.IntegerField(
        null=True, blank=True,
        help_text='X-MinLimit-Remaining from the most recently recorded Xero response.'
    )
    last_status = models.IntegerField(
        null=True, blank=True,
        help_text='HTTP status of the response the headers came from (e.g. 200, 429).'
    )
    seen_at = models.DateTimeField(help_text='When these headers were observed.')

    class Meta:
        verbose_name = 'Xero API quota'
        verbose_name_plural = 'Xero API quotas'

    def __str__(self):
        return f'{self.tenant_id}: day={self.day_remaining} min={self.min_remaining} @ {self.seen_at}'
