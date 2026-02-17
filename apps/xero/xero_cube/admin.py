from django.contrib import admin
from apps.xero.xero_cube.models import XeroTrailBalance, XeroBalanceSheet


@admin.register(XeroTrailBalance)
class XeroTrailBalanceAdmin(admin.ModelAdmin):
    list_display = ('organisation', 'account', 'year', 'month', 'contact', 'tracking1', 'tracking2', 'amount', 'balance_to_date')
    list_filter = ('organisation', 'contact', 'year', 'month', 'account__type', 'tracking1', 'tracking2')
    list_select_related = ('organisation', 'account', 'contact', 'tracking1', 'tracking2')
    search_fields = ('organisation__tenant_name', 'account__name', 'account__code', 'contact__name')
    readonly_fields = ('organisation', 'account', 'year', 'month', 'fin_year', 'fin_period')


@admin.register(XeroBalanceSheet)
class XeroBalanceSheetAdmin(admin.ModelAdmin):
    list_display = ('organisation', 'account', 'date', 'contact', 'amount', 'balance')
    list_filter = ('organisation', 'date', 'account__type')
    search_fields = ('organisation__tenant_name', 'account__name', 'account__code', 'contact__name')
    readonly_fields = ('organisation', 'account', 'date', 'year', 'month')
    date_hierarchy = 'date'
