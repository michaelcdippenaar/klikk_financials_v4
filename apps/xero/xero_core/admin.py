from django.contrib import admin
from apps.xero.xero_core.models import XeroTenant


@admin.register(XeroTenant)
class XeroTenantAdmin(admin.ModelAdmin):
    list_display = ('tenant_id', 'tenant_name', 'tracking_category_1_id', 'tracking_category_2_id')
    search_fields = ('tenant_id', 'tenant_name')
    readonly_fields = ('tenant_id',)
