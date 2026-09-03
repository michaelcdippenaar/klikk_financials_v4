"""Read-only admin for the activity trail — see models.ActivityEvent."""
from django.contrib import admin

from .models import ActivityEvent


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ('occurred_at', 'actor', 'actor_role', 'action',
                    'target_kind', 'target_id', 'target_ref', 'source')
    list_filter = ('action', 'target_kind', 'actor_role', 'source', 'occurred_at')
    search_fields = ('actor', 'action', 'target_id', 'target_ref', 'ip', 'request_id')
    date_hierarchy = 'occurred_at'
    ordering = ('-occurred_at',)
    readonly_fields = tuple(f.name for f in ActivityEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
