import datetime
from dataclasses import dataclass

from django.db.models import Max
from django.utils import timezone

from apps.web_api_v2.models import (
    IngestProcessRun,
    IngestSourceJobDefinition,
)
from apps.web_api_v2.types.ingest import IngestPeriodRunState, IngestValidationState
from apps.web_api_v2.types.source_connections import (
    SourceConnection,
    SourceConnectionAction,
    SourceConnectionActionKind,
    SourceConnectionAvailabilityCode,
    SourceConnectionCategory,
    SourceConnectionConfigurationState,
    SourceConnectionKey,
    SourceConnectionReadinessState,
    SourceConnections,
    SourceConnectionsSummary,
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
    unavailable: bool = False
    source_evidence_count: int | None = None
    source_evidence_at: datetime.datetime | None = None
    last_successful_run_at: datetime.datetime | None = None
    latest_v2_run_state: IngestPeriodRunState | None = None


def _xero_connection_evidence(entity, selected_periods):
    # Existence checks deliberately avoid reading OAuth/token payload columns.
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
        return XeroConnectionEvidence(configured=False)
    if entity.reauth_required:
        return XeroConnectionEvidence(configured=True, unavailable=True)

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
    latest_run = scoped_runs.order_by('-requested_at', '-id').only(
        'state',
    ).first()
    latest_success = scoped_runs.filter(
        state=IngestProcessRun.State.SUCCEEDED,
        finished_at__isnull=False,
    ).order_by('-finished_at', '-id').only('finished_at').first()
    return XeroConnectionEvidence(
        configured=True,
        source_evidence_count=source_evidence_count,
        source_evidence_at=source_evidence_at,
        last_successful_run_at=(latest_success.finished_at if latest_success else None),
        latest_v2_run_state=(RUN_STATE_MAP[latest_run.state] if latest_run else None),
    )


def _disabled_action(kind, reason):
    return SourceConnectionAction(
        kind=kind,
        permitted=False,
        reason=reason,
        required_capability=None,
        expected_state=None,
    )


def _xero_connection(evidence):
    actions = [
        _disabled_action(
            SourceConnectionActionKind.MANAGE_CONNECTION,
            'Xero connection management is not available through this read-only catalogue.',
        ),
        _disabled_action(
            SourceConnectionActionKind.SYNC,
            'Xero synchronization requires a separately approved V2 command.',
        ),
    ]
    if not evidence.configured:
        return SourceConnection(
            key=SourceConnectionKey.XERO,
            display_name='Xero',
            category=SourceConnectionCategory.ACCOUNTING,
            configuration_state=SourceConnectionConfigurationState.NOT_CONFIGURED,
            readiness_state=SourceConnectionReadinessState.NOT_CONFIGURED,
            availability_code=SourceConnectionAvailabilityCode.NOT_CONFIGURED,
            user_safe_reason='No entity-bound Xero connection is configured.',
            source_evidence_count=None,
            source_evidence_at=None,
            last_successful_run_at=None,
            validation_state=IngestValidationState.UNAVAILABLE,
            latest_v2_run_state=None,
            safe_identity=None,
            actions=actions,
        )
    if evidence.unavailable:
        return SourceConnection(
            key=SourceConnectionKey.XERO,
            display_name='Xero',
            category=SourceConnectionCategory.ACCOUNTING,
            configuration_state=SourceConnectionConfigurationState.CONFIGURED,
            readiness_state=SourceConnectionReadinessState.UNAVAILABLE,
            availability_code=SourceConnectionAvailabilityCode.UNAVAILABLE,
            user_safe_reason=(
                'The entity-bound Xero connection requires administrator attention.'
            ),
            source_evidence_count=None,
            source_evidence_at=None,
            last_successful_run_at=None,
            validation_state=IngestValidationState.UNAVAILABLE,
            latest_v2_run_state=None,
            safe_identity='Entity-bound Xero connection',
            actions=actions,
        )

    readiness = (
        SourceConnectionReadinessState.READY
        if evidence.source_evidence_count
        else SourceConnectionReadinessState.EMPTY
    )
    reason = (
        None
        if readiness == SourceConnectionReadinessState.READY
        else 'No local Xero source evidence is stored for this entity.'
    )
    return SourceConnection(
        key=SourceConnectionKey.XERO,
        display_name='Xero',
        category=SourceConnectionCategory.ACCOUNTING,
        configuration_state=SourceConnectionConfigurationState.CONFIGURED,
        readiness_state=readiness,
        availability_code=SourceConnectionAvailabilityCode.AVAILABLE,
        user_safe_reason=reason,
        source_evidence_count=evidence.source_evidence_count,
        source_evidence_at=evidence.source_evidence_at,
        last_successful_run_at=evidence.last_successful_run_at,
        validation_state=IngestValidationState.UNAVAILABLE,
        latest_v2_run_state=evidence.latest_v2_run_state,
        safe_identity='Entity-bound Xero connection',
        actions=actions,
    )


def _unavailable_connection(key, display_name, category, reason, action_kinds):
    return SourceConnection(
        key=key,
        display_name=display_name,
        category=category,
        configuration_state=SourceConnectionConfigurationState.UNAVAILABLE,
        readiness_state=SourceConnectionReadinessState.UNAVAILABLE,
        availability_code=SourceConnectionAvailabilityCode.UNAVAILABLE,
        user_safe_reason=reason,
        source_evidence_count=None,
        source_evidence_at=None,
        last_successful_run_at=None,
        validation_state=IngestValidationState.UNAVAILABLE,
        latest_v2_run_state=None,
        safe_identity=None,
        actions=[_disabled_action(kind, reason) for kind in action_kinds],
    )


def read_source_connections(entity, resolved_context):
    xero = _xero_connection(
        _xero_connection_evidence(entity, resolved_context.selected_periods),
    )
    connections = [
        xero,
        _unavailable_connection(
            SourceConnectionKey.INVESTEC_SHARE_TRADING,
            'Investec Share Trading',
            SourceConnectionCategory.INVESTMENTS,
            (
                'Share Trading status is unavailable until entity, account, and '
                'portfolio ownership is established.'
            ),
            (
                SourceConnectionActionKind.OPEN_WORKBENCH,
                SourceConnectionActionKind.SYNC,
            ),
        ),
        _unavailable_connection(
            SourceConnectionKey.WHATSAPP_RECEIPTS,
            'WhatsApp Receipts',
            SourceConnectionCategory.RECEIPTS,
            (
                'WhatsApp receipt status is unavailable until entity ownership '
                'and privacy rules are established.'
            ),
            (SourceConnectionActionKind.CONFIGURE, SourceConnectionActionKind.SYNC),
        ),
        _unavailable_connection(
            SourceConnectionKey.EMAIL_RECEIPTS,
            'Email Receipts',
            SourceConnectionCategory.RECEIPTS,
            (
                'Email receipt status is unavailable until provider, ownership, '
                'and credential custody are approved.'
            ),
            (SourceConnectionActionKind.CONFIGURE, SourceConnectionActionKind.SYNC),
        ),
        _unavailable_connection(
            SourceConnectionKey.PLANNING_ANALYTICS,
            'Planning Analytics',
            SourceConnectionCategory.DESTINATION,
            (
                'Planning Analytics readiness is unavailable until an '
                'entity-bound destination is approved.'
            ),
            (SourceConnectionActionKind.CONFIGURE, SourceConnectionActionKind.SUBMIT),
        ),
        _unavailable_connection(
            SourceConnectionKey.EXCEL_ADD_IN,
            'Excel Add-in',
            SourceConnectionCategory.IDENTITY,
            (
                'Excel Add-in identity is unavailable until its client and '
                'authentication boundary is approved.'
            ),
            (SourceConnectionActionKind.CONFIGURE_IDENTITY,),
        ),
    ]
    return SourceConnections(
        resolved_context=resolved_context,
        checked_at=timezone.now(),
        summary=SourceConnectionsSummary(
            total=len(connections),
            active=sum(
                item.availability_code == SourceConnectionAvailabilityCode.AVAILABLE
                for item in connections
            ),
            needs_setup=sum(
                item.configuration_state
                == SourceConnectionConfigurationState.NOT_CONFIGURED
                for item in connections
            ),
            ready_destinations=sum(
                item.category == SourceConnectionCategory.DESTINATION
                and item.readiness_state == SourceConnectionReadinessState.READY
                for item in connections
            ),
        ),
        connections=connections,
    )
