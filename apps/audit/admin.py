"""
Read-only admin for the comment-webhook delivery log.

The table is append-only by design (apps.audit.comment_webhook only ever
creates rows), and the admin is registered to match: it can be searched and
filtered, never added to, edited or deleted. An audit trail an operator can
quietly amend from the admin is not an audit trail.
"""
from django.contrib import admin

from .models import CommentWebhookDelivery


@admin.register(CommentWebhookDelivery)
class CommentWebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ('attempted_at', 'comment_kind', 'comment_id', 'author',
                    'status_code', 'short_error', 'target_url')
    list_filter = ('comment_kind', 'status_code', 'attempted_at')
    search_fields = ('author', 'target_url', 'error', 'response_snippet')
    date_hierarchy = 'attempted_at'
    ordering = ('-attempted_at',)
    readonly_fields = tuple(f.name for f in CommentWebhookDelivery._meta.fields)

    @admin.display(description='Error')
    def short_error(self, obj):
        return (obj.error or '')[:80]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
