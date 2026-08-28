from django.contrib import admin, messages
from django.utils import timezone

from apps.planning_analytics.models import EntityPlanningTarget


@admin.register(EntityPlanningTarget)
class EntityPlanningTargetAdmin(admin.ModelAdmin):
    """Bind an entity to a Planning Analytics destination, and approve it.

    Approval is a separate, deliberate act rather than a checkbox on the form:
    binding says where an entity's data would go, approving says it may go
    there. Recording who approved it, and when, is the point.
    """

    list_display = ('entity', 'display_name', 'workspace', 'active', 'approved_at', 'approved_by')
    list_filter = ('active', 'server')
    search_fields = ('display_name', 'workspace', 'entity__tenant_name')
    autocomplete_fields = ()
    readonly_fields = ('approved_at', 'approved_by', 'created_at', 'updated_at')
    actions = ('approve_targets', 'withdraw_approval')

    @admin.action(description='Approve selected destinations for their entity')
    def approve_targets(self, request, queryset):
        updated = queryset.filter(approved_at__isnull=True).update(
            approved_at=timezone.now(), approved_by=request.user,
        )
        skipped = queryset.count() - updated
        self.message_user(
            request,
            f'Approved {updated} destination(s).'
            + (f' {skipped} were already approved.' if skipped else ''),
            messages.SUCCESS,
        )

    @admin.action(description='Withdraw approval (stops submission immediately)')
    def withdraw_approval(self, request, queryset):
        updated = queryset.filter(approved_at__isnull=False).update(
            approved_at=None, approved_by=None,
        )
        self.message_user(request, f'Withdrew approval on {updated} destination(s).', messages.WARNING)
