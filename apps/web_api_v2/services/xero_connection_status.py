import datetime
from dataclasses import dataclass

from django.db.models import Max

from apps.web_api_v2.models import IngestProcessRun, IngestSourceJobDefinition
from apps.web_api_v2.types.ingest import IngestPeriodRunState
from apps.web_api_v2.types.xero_connection_status import (
    XeroConnectionActionDescriptor,
    XeroConnectionActionKind,
    XeroConnectionAuthorizationState,
    XeroConnectionAvailabilityCode,
    XeroConnectionStatus,
)
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_data.models import XeroTransactionSource
from apps.xero.xero_sync.models import XeroLastUpdate


XERO_PROCESS_KEYS = (
    'metadata',
    'transaction-journal-sync',
    'invoice-sync',
    'process-journals',
    'trail-balance',
)
RUN_STATE_MAP = {
    IngestProcessRun.State.QUEUED: IngestPeriodRunState.QUEUED,
    IngestProcessRun.State.RUNNING: IngestPeriodRunState.RUNNING,
    IngestProcessRun.State.SUCCEEDED: IngestPeriodRunState.SUCCEEDED,
    IngestProcessRun.State.FAILED: IngestPeriodRunState.FAILED,
    IngestProcessRun.State.CANCELLED: IngestPeriodRunState.CANCELLED,
    IngestProcessRun.State.BLOCKED: IngestPeriodRunState.BLOCKED,
}


@dataclass(frozen=True)
class XeroConnectionEvidence:
    configured: bool
    authorization_state: XeroConnectionAuthorizationState
    token_action_required: bool
    availability_code: XeroConnectionAvailabilityCode
    user_safe_reason: str | None
    source_evidence_count: int | None = None
    source_evidence_at: datetime.datetime | None = None
    last_successful_run_at: datetime.datetime | None = None
    latest_v2_run_state: IngestPeriodRunState | None = None


def read_xero_connection_evidence(entity, selected_periods):
    """Read credential-free, entity-bound Xero connection evidence."""
    canonical_token_lookup = f'tenant_tokens__{entity.pk}__token__isnull'
    has_bound_token = XeroClientCredentials.objects.filter(
        active=True,
        **{canonical_token_lookup: False},
    ).exists()
    has_configured_job = IngestSourceJobDefinition.objects.filter(
        entity=entity,
        source_family=IngestSourceJobDefinition.SourceFamily.XERO,
        configuration_state=IngestSourceJobDefinition.ConfigurationState.CONFIGURED,
        active=True,
        key__in=XERO_PROCESS_KEYS,
    ).exists()
    if not has_bound_token or not has_configured_job:
        return XeroConnectionEvidence(
            configured=False,
            authorization_state=XeroConnectionAuthorizationState.NOT_CONFIGURED,
            token_action_required=False,
            availability_code=XeroConnectionAvailabilityCode.NOT_CONFIGURED,
            user_safe_reason='No entity-bound Xero connection is configured.',
        )
    if entity.reauth_required:
        return XeroConnectionEvidence(
            configured=True,
            authorization_state=(
                XeroConnectionAuthorizationState.REAUTHORIZATION_REQUIRED
            ),
            token_action_required=True,
            availability_code=XeroConnectionAvailabilityCode.UNAVAILABLE,
            user_safe_reason=(
                'The entity-bound Xero connection requires administrator attention.'
            ),
        )

    source_evidence_count = XeroTransactionSource.objects.filter(
        organisation=entity,
    ).count()
    source_evidence_at = XeroLastUpdate.objects.filter(
        organisation=entity,
        date__isnull=False,
    ).aggregate(latest=Max('date'))['latest']

    periods = [str(period) for period in selected_periods]
    scoped_runs = IngestProcessRun.objects.filter(
        entity=entity,
        process_key__in=XERO_PROCESS_KEYS,
        run_periods__period__in=periods,
    ).distinct()
    latest_run = scoped_runs.order_by('-requested_at', '-id').only('state').first()
    latest_success = scoped_runs.filter(
        state=IngestProcessRun.State.SUCCEEDED,
        finished_at__isnull=False,
    ).order_by('-finished_at', '-id').only('finished_at').first()
    return XeroConnectionEvidence(
        configured=True,
        authorization_state=XeroConnectionAuthorizationState.AUTHORIZED,
        token_action_required=False,
        availability_code=XeroConnectionAvailabilityCode.AVAILABLE,
        user_safe_reason=(
            None
            if source_evidence_count
            else 'No local Xero source evidence is stored for this entity.'
        ),
        source_evidence_count=source_evidence_count,
        source_evidence_at=source_evidence_at,
        last_successful_run_at=(latest_success.finished_at if latest_success else None),
        latest_v2_run_state=(RUN_STATE_MAP[latest_run.state] if latest_run else None),
    )


def _disabled_action(kind, reason):
    return XeroConnectionActionDescriptor(
        kind=kind,
        permitted=False,
        reason=reason,
        required_capability=None,
        expected_state=None,
    )


def build_xero_connection_status(entity, resolved_context):
    evidence = read_xero_connection_evidence(
        entity,
        resolved_context.selected_periods,
    )
    actions = [
        _disabled_action(
            XeroConnectionActionKind.MANAGE_CONNECTION,
            (
                'Xero connection management is not available through this '
                'read-only status contract.'
            ),
        ),
        _disabled_action(
            XeroConnectionActionKind.SYNC,
            'Xero synchronization requires a separately approved V2 command.',
        ),
    ]
    return XeroConnectionStatus(
        resolved_context=resolved_context,
        configured=evidence.configured,
        authorization_state=evidence.authorization_state,
        token_action_required=evidence.token_action_required,
        source_evidence_at=evidence.source_evidence_at,
        last_successful_run_at=evidence.last_successful_run_at,
        availability_code=evidence.availability_code,
        user_safe_reason=evidence.user_safe_reason,
        actions=actions,
    )
