from django.contrib import admin

from apps.investec.models import InvestecEntityAccount, InvestecJseShareNameMapping


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


@admin.register(InvestecJseShareNameMapping)
class InvestecJseShareNameMappingAdmin(admin.ModelAdmin):
    """Attach the names a share arrives under to its share code.

    The share account is loaded by uploading statements, and the same
    instrument appears under different spellings on different ones — "A V I",
    "AVI", "JSE.AVI" are one share. A name with no mapping leaves its
    transactions unattributable to a holding, which is why the workbench
    reports the gap.

    share_code is uniquely constrained, so a second spelling goes in
    share_name2 or share_name3 on the EXISTING row for that code. Creating a
    new row for the same code will be rejected.
    """

    list_display = ('share_name', 'share_name2', 'share_name3', 'share_code', 'company')
    list_editable = ('share_name2', 'share_name3')
    list_filter = ('share_code',)
    search_fields = ('share_name', 'share_name2', 'share_name3', 'share_code', 'company')
    ordering = ('share_name',)
    list_per_page = 100
    readonly_fields = ('created_at', 'updated_at')
