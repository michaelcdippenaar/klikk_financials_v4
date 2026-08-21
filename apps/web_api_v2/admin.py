from django.contrib import admin

from .models import (
    IngestProcessAuditEvent,
    IngestProcessRun,
    IngestSourceJobDefinition,
    UserEntityCapability,
    UserEntityMembership,
    ViewerPreference,
)


@admin.register(UserEntityMembership)
class UserEntityMembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'entity', 'role', 'active', 'updated_at')
    list_filter = ('active', 'role')
    search_fields = ('user__username', 'user__email', 'entity__tenant_id', 'entity__tenant_name')
    autocomplete_fields = ('user', 'entity')


@admin.register(ViewerPreference)
class ViewerPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'default_entity', 'default_financial_year', 'updated_at')
    search_fields = ('user__username', 'user__email')
    autocomplete_fields = ('user', 'default_entity')


@admin.register(UserEntityCapability)
class UserEntityCapabilityAdmin(admin.ModelAdmin):
    list_display = ('membership', 'code', 'active', 'granted_by', 'updated_at')
    list_filter = ('code', 'active')
    search_fields = ('membership__user__username', 'membership__entity__tenant_name')
    autocomplete_fields = ('membership', 'granted_by')


@admin.register(IngestSourceJobDefinition)
class IngestSourceJobDefinitionAdmin(admin.ModelAdmin):
    list_display = ('entity', 'key', 'label', 'source_family', 'required', 'configuration_state', 'active')
    list_filter = ('source_family', 'required', 'configuration_state', 'active')
    search_fields = ('entity__tenant_id', 'entity__tenant_name', 'key', 'label')
    autocomplete_fields = ('entity',)


@admin.register(IngestProcessRun)
class IngestProcessRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'entity', 'process_key', 'state', 'actor', 'requested_at', 'finished_at')
    list_filter = ('process_key', 'state')
    search_fields = ('id', 'entity__tenant_id', 'entity__tenant_name', 'actor__username')
    readonly_fields = tuple(field.name for field in IngestProcessRun._meta.fields)


@admin.register(IngestProcessAuditEvent)
class IngestProcessAuditEventAdmin(admin.ModelAdmin):
    list_display = ('run', 'entity', 'process_key', 'action', 'result_state', 'actor', 'occurred_at')
    list_filter = ('process_key', 'action', 'result_state')
    search_fields = ('run__id', 'entity__tenant_id', 'actor__username')
    readonly_fields = tuple(field.name for field in IngestProcessAuditEvent._meta.fields)
