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
