"""
Admin for the equipment price list.

Prices are shown inline on the item; adding a row here bypasses
``services.add_price`` (no auto-close of the previous open row), so the
exclusion constraint is what stops an overlapping general LIST price — the
admin will show the IntegrityError. Prefer the API / seed command for edits.
"""
from django.contrib import admin

from .models import PriceListItem, PriceListPrice


class PriceListPriceInline(admin.TabularInline):
    model = PriceListPrice
    extra = 0
    fields = ('price', 'price_type', 'customer', 'valid_from', 'valid_to', 'note', 'set_by')
    raw_id_fields = ('customer',)
    ordering = ('-valid_from', '-id')


@admin.register(PriceListItem)
class PriceListItemAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'category', 'unit', 'qty_owned', 'active', 'xero_account_code')
    list_filter = ('category', 'unit', 'active')
    search_fields = ('code', 'name', 'description', 'notes')
    raw_id_fields = ('xero_purchase_line',)
    readonly_fields = ('created_at', 'updated_at')
    inlines = [PriceListPriceInline]


@admin.register(PriceListPrice)
class PriceListPriceAdmin(admin.ModelAdmin):
    list_display = ('item', 'price', 'price_type', 'customer', 'valid_from', 'valid_to', 'set_by')
    list_filter = ('price_type', 'valid_from')
    search_fields = ('item__code', 'item__name', 'note')
    # XeroContacts admin declares search_fields, so autocomplete works; item is ours.
    autocomplete_fields = ('customer', 'item')
    readonly_fields = ('created_at',)
