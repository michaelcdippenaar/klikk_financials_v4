from django.conf import settings
from django.db import models

from apps.xero.xero_core.models import XeroTenant


class KnowledgeCorpus(models.Model):
    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_ai_knowledge_corpora',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f'KnowledgeCorpus<{self.id}> {self.slug}'


class SystemDocument(models.Model):
    project = models.ForeignKey(
        'AgentProject',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='system_documents',
    )
    corpus = models.ForeignKey(
        KnowledgeCorpus,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='system_documents',
    )
    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=255, blank=True, default='')
    content_markdown = models.TextField(blank=True, default='')
    pin_to_context = models.BooleanField(default=False)
    context_order = models.IntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_system_documents',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['is_active']),
            models.Index(fields=['project', 'pin_to_context', 'context_order']),
            models.Index(fields=['corpus', 'project']),
        ]

    def __str__(self):
        return f'SystemDocument<{self.id}> {self.slug}'


class GlossaryRefreshRequest(models.Model):
    """
    Singleton (single row) used to request that account/contact glossary docs be refreshed
    after Xero metadata changes. A management command or cron runs refresh and clears this.
    """
    requested_at = models.DateTimeField(auto_now=True)
    organisation_id = models.IntegerField(
        null=True,
        blank=True,
        help_text='XeroTenant id that changed; null = refresh for all orgs',
    )

    class Meta:
        app_label = 'ai_agent'

    def __str__(self):
        return f'GlossaryRefreshRequest at {self.requested_at}'


class AgentProject(models.Model):
    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    memory = models.JSONField(default=dict, blank=True)
    default_corpus = models.ForeignKey(
        KnowledgeCorpus,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_for_projects',
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_ai_agent_projects',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f'AgentProject<{self.id}> {self.slug}'


class KnowledgeChunkEmbedding(models.Model):
    """
    Chunk-level embeddings for SystemDocument content (and exported chat transcripts).
    Stored as JSON for portability (no pgvector requirement).
    """
    corpus = models.ForeignKey(KnowledgeCorpus, on_delete=models.CASCADE, related_name='chunks')
    project = models.ForeignKey('AgentProject', on_delete=models.CASCADE, related_name='knowledge_chunks', null=True, blank=True)
    system_document = models.ForeignKey(SystemDocument, on_delete=models.CASCADE, related_name='knowledge_chunks')

    embedding_model = models.CharField(max_length=120, default='text-embedding-3-small')
    source_hash = models.CharField(max_length=64, db_index=True)
    chunk_index = models.PositiveIntegerField()
    chunk_text = models.TextField()
    embedding = models.JSONField(default=list, blank=True)  # list[float]
    embedded_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['system_document_id', 'chunk_index']
        unique_together = (('system_document', 'embedding_model', 'chunk_index'),)
        indexes = [
            models.Index(fields=['corpus', 'project', 'system_document']),
            models.Index(fields=['corpus', 'embedding_model']),
        ]

    def __str__(self):
        return f'KnowledgeChunkEmbedding<{self.id}> doc={self.system_document_id} idx={self.chunk_index}'


class AgentSession(models.Model):
    STATUS_OPEN = 'open'
    STATUS_CLOSED = 'closed'
    STATUS_ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_CLOSED, 'Closed'),
        (STATUS_ARCHIVED, 'Archived'),
    ]

    organisation = models.ForeignKey(
        XeroTenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='ai_agent_sessions',
    )
    project = models.ForeignKey(
        AgentProject,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sessions',
    )
    title = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    memory = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_ai_agent_sessions',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']

    def __str__(self):
        return f'AgentSession<{self.id}> {self.title or "Untitled"}'


class AgentMessage(models.Model):
    ROLE_SYSTEM = 'system'
    ROLE_USER = 'user'
    ROLE_ASSISTANT = 'assistant'
    ROLE_TOOL = 'tool'
    ROLE_CHOICES = [
        (ROLE_SYSTEM, 'System'),
        (ROLE_USER, 'User'),
        (ROLE_ASSISTANT, 'Assistant'),
        (ROLE_TOOL, 'Tool'),
    ]

    session = models.ForeignKey(AgentSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_agent_messages',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'AgentMessage<{self.id}> {self.role}'


class AgentToolExecutionLog(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_SUCCESS = 'success'
    STATUS_ERROR = 'error'
    STATUS_BLOCKED = 'blocked'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SUCCESS, 'Success'),
        (STATUS_ERROR, 'Error'),
        (STATUS_BLOCKED, 'Blocked'),
    ]

    session = models.ForeignKey(
        AgentSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tool_executions',
    )
    message = models.ForeignKey(
        AgentMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tool_executions',
    )
    tool_name = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    input_payload = models.JSONField(default=dict, blank=True)
    output_payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_agent_tool_executions',
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at', '-id']

    def __str__(self):
        return f'AgentToolExecutionLog<{self.id}> {self.tool_name} {self.status}'


class AgentApprovalRequest(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    session = models.ForeignKey(AgentSession, on_delete=models.CASCADE, related_name='approval_requests')
    tool_execution = models.OneToOneField(
        AgentToolExecutionLog,
        on_delete=models.CASCADE,
        related_name='approval_request',
        null=True,
        blank=True,
    )
    action_name = models.CharField(max_length=120)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_agent_approval_requests',
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_agent_approvals_reviewed',
    )
    review_note = models.TextField(blank=True, default='')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'AgentApprovalRequest<{self.id}> {self.action_name} {self.status}'


# ---------------------------------------------------------------------------
# MCP Skills Engine models (migrated from FastAPI klikk-ai-portal)
# ---------------------------------------------------------------------------

class SkillRegistry(models.Model):
    """Registry of available AI agent skill modules.

    Each row represents a Python skill module (e.g. investment_analyst)
    with its routing keywords, enabled/disabled state, and metadata.
    """
    module_name = models.CharField(max_length=120, unique=True, help_text='e.g. "web_search"')
    import_path = models.CharField(max_length=255, help_text='e.g. "apps.ai_agent.skills.web_search"')
    display_name = models.CharField(max_length=255, blank=True, default='')
    description = models.TextField(blank=True, default='')
    keywords = models.JSONField(default=list, blank=True, help_text='Routing keywords (list of strings)')
    always_on = models.BooleanField(default=False, help_text='Always include in tool routing')
    enabled = models.BooleanField(default=True, help_text='Load this skill at startup')
    sort_order = models.IntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'module_name']
        verbose_name = 'Skill Registry Entry'
        verbose_name_plural = 'Skill Registry'

    def __str__(self):
        status = 'ON' if self.enabled else 'OFF'
        return f'{self.display_name or self.module_name} [{status}]'


class Credential(models.Model):
    """DB-backed credential/secret storage.

    API keys and tokens stored here take precedence over .env values.
    The agent/config.py adapter checks this model first with a 60s cache.
    """
    key = models.CharField(max_length=120, unique=True, help_text='e.g. "anthropic_api_key"')
    value = models.TextField(default='', blank=True)
    label = models.CharField(max_length=255, blank=True, default='', help_text='Human-readable label')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return f'{self.label or self.key}'

    @property
    def masked_value(self):
        v = self.value or ''
        if len(v) > 16:
            return v[:8] + '...' + v[-4:]
        return '***' if v else ''


class GlobalContext(models.Model):
    """Stored facts and context for the AI agent (semantic search via pgvector on bi_etl).

    Non-vector fields live here in Django ORM; the embedding column in
    klikk_bi_etl.agent_rag.global_context is queried via raw SQL.
    """
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    embedding = models.JSONField(default=list, blank=True, help_text='Embedding vector as list of floats')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'GlobalContext<{self.id}> {self.content[:60]}'


class ConversationContext(models.Model):
    """Stored conversation turns for cross-session semantic search."""
    session = models.ForeignKey(
        AgentSession,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='conversation_contexts',
    )
    session_external_id = models.CharField(
        max_length=120, blank=True, default='',
        help_text='Session ID from external source (e.g. FastAPI chat)',
    )
    role = models.CharField(max_length=20, blank=True, default='')
    content = models.TextField()
    embedding = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'ConversationContext<{self.id}> {self.role} {self.content[:40]}'


class AgentViewState(models.Model):
    """Persisted PAW view query state for the agent (cube/server/MDX context)."""
    session = models.ForeignKey(
        AgentSession,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='view_states',
    )
    cube_name = models.CharField(max_length=255)
    server_name = models.CharField(max_length=255, blank=True, default='')
    query_state = models.TextField(blank=True, default='', help_text='JSON-encoded PAW query state')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (('session', 'cube_name', 'server_name'),)

    def __str__(self):
        return f'AgentViewState<{self.id}> {self.cube_name}'

