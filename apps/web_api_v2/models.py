import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.xero.xero_core.models import XeroTenant


class UserEntityMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = 'OWNER', 'Owner'
        ADMIN = 'ADMIN', 'Administrator'
        REVIEWER = 'REVIEWER', 'Reviewer'
        VIEWER = 'VIEWER', 'Viewer'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='entity_memberships',
    )
    entity = models.ForeignKey(
        XeroTenant,
        on_delete=models.CASCADE,
        related_name='user_memberships',
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'entity'),
                name='web_api_v2_unique_user_entity',
            ),
        ]
        indexes = [
            models.Index(
                fields=('user', 'active'),
                name='web_api_v2_user_active_idx',
            ),
        ]
        ordering = ('entity__tenant_name', 'entity_id')

    def __str__(self):
        return f'{self.user} -> {self.entity} ({self.role})'


class ViewerPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='web_api_v2_preferences',
    )
    default_entity = models.ForeignKey(
        XeroTenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    default_financial_year = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=(MinValueValidator(1900), MaxValueValidator(9999)),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Web preferences for {self.user}'


class UserEntityCapability(models.Model):
    class Code(models.TextChoices):
        RUN_INGESTION_PROCESS = 'RUN_INGESTION_PROCESS', 'Run ingestion process'

    membership = models.ForeignKey(
        UserEntityMembership,
        on_delete=models.CASCADE,
        related_name='capability_grants',
    )
    code = models.CharField(max_length=64, choices=Code.choices)
    active = models.BooleanField(default=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('membership', 'code'),
                name='web_api_v2_unique_membership_capability',
            ),
        ]
        indexes = [
            models.Index(
                fields=('membership', 'active'),
                name='web_api_v2_cap_active_idx',
            ),
        ]


class IngestSourceJobDefinition(models.Model):
    class SourceFamily(models.TextChoices):
        XERO = 'XERO', 'Xero'
        INVESTEC = 'INVESTEC', 'Investec'
        MANUAL = 'MANUAL', 'Manual'
        PLANNING_ANALYTICS = 'PLANNING_ANALYTICS', 'Planning Analytics'

    class ConfigurationState(models.TextChoices):
        CONFIGURED = 'CONFIGURED', 'Configured'
        NOT_CONFIGURED = 'NOT_CONFIGURED', 'Not configured'
        DISABLED = 'DISABLED', 'Disabled'

    entity = models.ForeignKey(
        XeroTenant,
        on_delete=models.CASCADE,
        related_name='ingest_source_job_definitions',
    )
    key = models.CharField(max_length=64)
    source_family = models.CharField(max_length=32, choices=SourceFamily.choices)
    label = models.CharField(max_length=160)
    required = models.BooleanField(default=True)
    configuration_state = models.CharField(
        max_length=32,
        choices=ConfigurationState.choices,
        default=ConfigurationState.CONFIGURED,
    )
    supported_operations = models.JSONField(default=list, blank=True)
    read_capabilities = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('entity', 'key'),
                name='web_api_v2_unique_entity_source_job',
            ),
        ]
        indexes = [
            models.Index(
                fields=('entity', 'active'),
                name='web_api_v2_source_active_idx',
            ),
        ]
        ordering = ('entity_id', 'pk')


class IngestProcessRun(models.Model):
    class State(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        BLOCKED = 'blocked', 'Blocked'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.ForeignKey(
        XeroTenant,
        on_delete=models.CASCADE,
        related_name='ingest_process_runs',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='ingest_process_runs',
    )
    process_key = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    idempotency_key = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)
    periods = models.JSONField(default=list, blank=True)
    records_summary = models.JSONField(default=dict, blank=True)
    output_summary = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=240, blank=True)
    retryable = models.BooleanField(default=False)
    blocked_reason = models.JSONField(null=True, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)
    retry_of = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retries',
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('entity', 'idempotency_key'),
                name='web_api_v2_unique_entity_idempotency',
            ),
            models.UniqueConstraint(
                fields=('entity',),
                condition=Q(state__in=('queued', 'running')),
                name='web_api_v2_one_active_entity_ingest',
            ),
        ]
        indexes = [
            models.Index(
                fields=('entity', 'process_key', '-requested_at'),
                name='web_api_v2_run_process_idx',
            ),
            models.Index(
                fields=('entity', 'state', '-requested_at'),
                name='web_api_v2_run_state_idx',
            ),
        ]
        ordering = ('-requested_at', '-id')


class IngestProcessAuditEvent(models.Model):
    run = models.ForeignKey(
        IngestProcessRun,
        on_delete=models.CASCADE,
        related_name='audit_events',
    )
    entity = models.ForeignKey(XeroTenant, on_delete=models.PROTECT, related_name='+')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    process_key = models.CharField(max_length=64)
    action = models.CharField(max_length=32)
    result_state = models.CharField(max_length=16, choices=IngestProcessRun.State.choices)
    correlation_id = models.UUIDField()
    safe_metadata = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=('entity', '-occurred_at'),
                name='web_api_v2_audit_entity_idx',
            ),
        ]
        ordering = ('occurred_at', 'pk')
