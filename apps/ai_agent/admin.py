from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .models import (
    AgentApprovalRequest,
    AgentMessage,
    AgentProject,
    AgentSession,
    AgentToolExecutionLog,
    KnowledgeChunkEmbedding,
    KnowledgeCorpus,
    SystemDocument,
)


@admin.register(AgentSession)
class AgentSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'project', 'organisation', 'status', 'created_by', 'updated_at')
    list_filter = ('status', 'project', 'organisation')
    search_fields = ('title', 'project__slug', 'project__name', 'organisation__tenant_name', 'organisation__tenant_id', 'created_by__username')


@admin.register(AgentMessage)
class AgentMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'session', 'role', 'created_by', 'created_at')
    list_filter = ('role',)
    search_fields = ('session__title', 'content')


@admin.register(AgentToolExecutionLog)
class AgentToolExecutionLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'tool_name', 'status', 'session', 'executed_by', 'started_at', 'finished_at')
    list_filter = ('status', 'tool_name')
    search_fields = ('tool_name', 'error_message', 'session__title')


@admin.register(AgentApprovalRequest)
class AgentApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'action_name', 'status', 'session', 'requested_by', 'reviewed_by', 'created_at')
    list_filter = ('status', 'action_name')
    search_fields = ('action_name', 'session__title', 'requested_by__username')


@admin.register(SystemDocument)
class SystemDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'slug', 'title', 'corpus', 'project', 'pin_to_context', 'context_order', 'is_active', 'updated_at', 'export_markdown_link')
    list_filter = ('is_active', 'corpus', 'project', 'pin_to_context')
    search_fields = ('slug', 'title', 'content_markdown')
    readonly_fields = ('created_at', 'updated_at', 'created_by')

    def save_model(self, request, obj, form, change):
        if not obj.created_by and getattr(request.user, 'is_authenticated', False):
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description='Export')
    def export_markdown_link(self, obj):
        url = reverse('admin:ai_agent_systemdocument_export_markdown', args=[obj.pk])
        return format_html('<a class="button" href="{}">Export .md</a>', url)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                '<int:pk>/export-markdown/',
                self.admin_site.admin_view(self.export_markdown_view),
                name='ai_agent_systemdocument_export_markdown',
            ),
        ]
        return custom + urls

    def export_markdown_view(self, request, pk: int):
        obj = self.get_object(request, pk)
        if not obj:
            return HttpResponse('Not found', status=404, content_type='text/plain')

        filename = f'{obj.slug or "system-document"}.md'
        response = HttpResponse(
            obj.content_markdown or '',
            content_type='text/markdown; charset=utf-8',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


@admin.register(AgentProject)
class AgentProjectAdmin(admin.ModelAdmin):
    list_display = ('id', 'slug', 'name', 'is_active', 'updated_at', 'created_by')
    list_filter = ('is_active',)
    search_fields = ('slug', 'name', 'description')
    readonly_fields = ('created_at', 'updated_at', 'created_by')

    def save_model(self, request, obj, form, change):
        if not obj.created_by and getattr(request.user, 'is_authenticated', False):
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(KnowledgeCorpus)
class KnowledgeCorpusAdmin(admin.ModelAdmin):
    list_display = ('id', 'slug', 'name', 'is_active', 'updated_at', 'created_by')
    list_filter = ('is_active',)
    search_fields = ('slug', 'name', 'description')
    readonly_fields = ('created_at', 'updated_at', 'created_by')

    def save_model(self, request, obj, form, change):
        if not obj.created_by and getattr(request.user, 'is_authenticated', False):
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(KnowledgeChunkEmbedding)
class KnowledgeChunkEmbeddingAdmin(admin.ModelAdmin):
    list_display = ('id', 'corpus', 'project', 'system_document', 'embedding_model', 'chunk_index', 'embedded_at')
    list_filter = ('embedding_model', 'corpus')
    search_fields = ('system_document__slug', 'system_document__title', 'chunk_text')

