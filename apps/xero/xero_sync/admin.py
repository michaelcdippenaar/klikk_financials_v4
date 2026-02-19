from django.contrib import admin
from apps.xero.xero_sync.models import (
    XeroLastUpdate, XeroTenantSchedule, XeroTaskExecutionLog, XeroApiCallLog,
    ProcessTree, Trigger, ProcessTreeSchedule
)


@admin.register(XeroApiCallLog)
class XeroApiCallLogAdmin(admin.ModelAdmin):
    list_display = ('process', 'tenant', 'api_calls', 'created_at')
    list_filter = ('process', 'created_at')
    search_fields = ('process', 'tenant__tenant_name')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


@admin.register(XeroLastUpdate)
class XeroLastUpdateAdmin(admin.ModelAdmin):
    list_display = ('organisation', 'end_point', 'date', 'name')
    list_filter = ('end_point', 'organisation')
    search_fields = ('organisation__tenant_name', 'end_point', 'name')
    readonly_fields = ('organisation', 'end_point')
    fields = ('organisation', 'end_point', 'date', 'name')


@admin.register(XeroTenantSchedule)
class XeroTenantScheduleAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'enabled', 'update_interval_minutes', 'last_update_run', 'next_update_run')
    list_filter = ('enabled', 'update_interval_minutes')
    search_fields = ('tenant__tenant_name', 'tenant__tenant_id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(XeroTaskExecutionLog)
class XeroTaskExecutionLogAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'task_type', 'status', 'started_at', 'completed_at', 'duration_seconds', 'records_processed')
    list_filter = ('task_type', 'status', 'started_at')
    search_fields = ('tenant__tenant_name', 'tenant__tenant_id', 'error_message')
    readonly_fields = ('started_at', 'completed_at', 'created_at')
    date_hierarchy = 'started_at'


@admin.register(Trigger)
class TriggerAdmin(admin.ModelAdmin):
    """Admin interface for Trigger model."""
    list_display = (
        'name', 'trigger_type', 'enabled', 'process_tree', 
        'xero_last_update', 'trigger_count', 'last_triggered', 'created_at'
    )
    list_filter = ('trigger_type', 'enabled', 'process_tree', 'created_at')
    search_fields = ('name', 'description', 'process_tree__name')
    readonly_fields = ('last_checked', 'last_triggered', 'trigger_count', 'created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'trigger_type', 'enabled', 'description')
        }),
        ('Configuration', {
            'fields': ('configuration', 'xero_last_update', 'process_tree'),
            'description': 'Configure trigger parameters based on trigger type'
        }),
        ('Statistics', {
            'fields': ('last_checked', 'last_triggered', 'trigger_count'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_readonly_fields(self, request, obj=None):
        """Make created_at and updated_at readonly."""
        return self.readonly_fields


@admin.register(ProcessTreeSchedule)
class ProcessTreeScheduleAdmin(admin.ModelAdmin):
    """Admin interface for ProcessTreeSchedule model."""
    list_display = (
        'process_tree', 'enabled', 'interval_minutes', 
        'last_run', 'next_run', 'created_at'
    )
    list_filter = ('enabled', 'created_at')
    search_fields = ('process_tree__name',)
    readonly_fields = ('last_run', 'next_run', 'created_at', 'updated_at')
    fieldsets = (
        ('Schedule', {
            'fields': ('process_tree', 'enabled', 'interval_minutes', 'start_time')
        }),
        ('Execution', {
            'fields': ('last_run', 'next_run', 'context')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ProcessTree)
class ProcessTreeAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'enabled', 'cache_enabled', 'created_at', 'updated_at')
    list_filter = ('enabled', 'cache_enabled', 'created_at')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')
    filter_horizontal = ('dependent_trees', 'sibling_trees')
    fields = ('name', 'description', 'process_tree_data', 'response_variables', 'cache_enabled', 'enabled', 'dependent_trees', 'sibling_trees', 'created_at', 'updated_at')
