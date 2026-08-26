from django.utils import timezone

from apps.web_api_v2.services.xero_connection_status import (
    read_xero_connection_evidence,
)
from apps.web_api_v2.types.ingest import IngestValidationState
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
from apps.web_api_v2.types.xero_connection_status import (
    XeroConnectionAuthorizationState,
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
    if (
        evidence.authorization_state
        == XeroConnectionAuthorizationState.REAUTHORIZATION_REQUIRED
    ):
        return SourceConnection(
            key=SourceConnectionKey.XERO,
            display_name='Xero',
            category=SourceConnectionCategory.ACCOUNTING,
            configuration_state=SourceConnectionConfigurationState.CONFIGURED,
            readiness_state=SourceConnectionReadinessState.UNAVAILABLE,
            availability_code=SourceConnectionAvailabilityCode.UNAVAILABLE,
            user_safe_reason=evidence.user_safe_reason,
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
        user_safe_reason=evidence.user_safe_reason or reason,
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
        read_xero_connection_evidence(entity, resolved_context.selected_periods),
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
