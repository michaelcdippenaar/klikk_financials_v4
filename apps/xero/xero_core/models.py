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
