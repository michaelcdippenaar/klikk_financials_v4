from django.contrib import admin

from apps.investec.models import InvestecEntityAccount


@admin.register(InvestecEntityAccount)
class InvestecEntityAccountAdmin(admin.ModelAdmin):
    """Attribute an Investec account to the entity whose books it belongs to.

    This used to be two dictionaries in code, so recording an ownership fact
    needed a release. It is still deliberate: an entity with no rows sees no
    Investec data at all, rather than inheriting somebody else's account.
    """

    list_display = ('entity', 'kind', 'account_number', 'label', 'active', 'updated_at')
    list_filter = ('kind', 'active')
    search_fields = ('account_number', 'label', 'entity__tenant_name')
    readonly_fields = ('created_at', 'updated_at')
