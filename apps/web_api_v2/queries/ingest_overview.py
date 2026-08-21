import strawberry
from graphql import GraphQLError

from apps.web_api_v2.models import IngestProcessRun, IngestSourceJobDefinition
from apps.web_api_v2.services.entity_access import (
    RUN_INGESTION_PROCESS_CAPABILITY,
    capability_codes_for_membership,
    require_entity_access,
)
from apps.web_api_v2.services.ingest_registry import has_tenant_credentials, prerequisite_status
from apps.web_api_v2.types.ingest import (
    IngestBlockedReason,
    IngestConnectionState,
    IngestNextAction,
    IngestOverview,
    IngestPeriodRunState,
    IngestPeriodRunSummary,
    IngestPrerequisite,
    IngestRecordsSummary,
    IngestSourceFamily,
    IngestSourceJobDefinition as IngestSourceJobDefinitionType,
    IngestStageState,
    IngestValidation,
    IngestValidationState,
    YearMonthValue,
)


RUN_STATE_MAP = {
    IngestProcessRun.State.QUEUED: IngestPeriodRunState.QUEUED,
    IngestProcessRun.State.RUNNING: IngestPeriodRunState.RUNNING,
    IngestProcessRun.State.SUCCEEDED: IngestPeriodRunState.SUCCEEDED,
    IngestProcessRun.State.FAILED: IngestPeriodRunState.FAILED,
    IngestProcessRun.State.CANCELLED: IngestPeriodRunState.CANCELLED,
    IngestProcessRun.State.BLOCKED: IngestPeriodRunState.BLOCKED,
}


def _iso(value):
    return value


def _connection_state(definition, entity):
    if definition.configuration_state != IngestSourceJobDefinition.ConfigurationState.CONFIGURED:
        return IngestConnectionState.NOT_CONFIGURED
    if entity.reauth_required:
        return IngestConnectionState.TEMPORARILY_UNAVAILABLE
    if not has_tenant_credentials(entity.pk):
        return IngestConnectionState.NOT_CONFIGURED
    return IngestConnectionState.CONFIGURED


def _latest_success(entity, process_key):
    return IngestProcessRun.objects.filter(
        entity=entity,
        process_key=process_key,
        state=IngestProcessRun.State.SUCCEEDED,
    ).order_by('-finished_at').first()


def _records(run):
    values = run.records_summary if run else {}
    return IngestRecordsSummary(
        read=values.get('read') or values.get('records') or values.get('processed'),
        created=values.get('created'),
        updated=values.get('updated'),
        skipped=values.get('skipped'),
        failed=values.get('failed') or values.get('errors'),
    )


def _blocked_reason(code, message):
    return IngestBlockedReason(code=code, message=message)


def _summary(definition, entity, period, can_run):
    connection_state = _connection_state(definition, entity)
    latest = IngestProcessRun.objects.filter(
        entity=entity,
        process_key=definition.key,
    ).order_by('-requested_at').first()
    latest_success = _latest_success(entity, definition.key)
    prerequisites = prerequisite_status(entity, definition.key)
    typed_prerequisites = [IngestPrerequisite(**item) for item in prerequisites]
    failed_prerequisite = next((item for item in prerequisites if not item['satisfied']), None)
    blocked_reason = None

    if connection_state == IngestConnectionState.NOT_CONFIGURED:
        state = IngestPeriodRunState.NOT_CONFIGURED
        validation_state = IngestValidationState.NOT_RUN
        next_action = IngestNextAction.CONFIGURE
        blocked_reason = _blocked_reason('NOT_CONFIGURED', 'This source is not configured.')
    elif connection_state == IngestConnectionState.TEMPORARILY_UNAVAILABLE:
        state = IngestPeriodRunState.TEMPORARILY_UNAVAILABLE
        validation_state = IngestValidationState.FAILED
        next_action = IngestNextAction.REAUTHORIZE
        blocked_reason = _blocked_reason(
            'XERO_REAUTHORIZATION_REQUIRED',
            'The Xero connection must be re-authorized.',
        )
    elif latest is None:
        state = IngestPeriodRunState.NOT_RUN
        validation_state = IngestValidationState.NOT_RUN
        next_action = IngestNextAction.RUN if can_run and not failed_prerequisite else IngestNextAction.NONE
    elif latest.periods and str(period) not in latest.periods:
        state = IngestPeriodRunState.NO_PERIOD_DATA
        validation_state = IngestValidationState.NOT_RUN
        next_action = IngestNextAction.RUN if can_run and not failed_prerequisite else IngestNextAction.NONE
    else:
        state = RUN_STATE_MAP[latest.state]
        validation_state = (
            IngestValidationState.PASSED
            if latest.state == IngestProcessRun.State.SUCCEEDED
            else IngestValidationState.FAILED
            if latest.state in (IngestProcessRun.State.FAILED, IngestProcessRun.State.BLOCKED)
            else IngestValidationState.NOT_RUN
        )
        if latest.state in (IngestProcessRun.State.QUEUED, IngestProcessRun.State.RUNNING):
            next_action = IngestNextAction.WAIT
        elif latest.state in (IngestProcessRun.State.FAILED, IngestProcessRun.State.BLOCKED):
            next_action = (
                IngestNextAction.RETRY
                if can_run and latest.retryable and not failed_prerequisite
                else IngestNextAction.NONE
            )
        else:
            next_action = IngestNextAction.RUN if can_run and not failed_prerequisite else IngestNextAction.NONE
        if latest.blocked_reason:
            blocked_reason = _blocked_reason(
                latest.blocked_reason.get('code', 'BLOCKED'),
                latest.blocked_reason.get('message', 'This source job is blocked.'),
            )

    if blocked_reason is None and failed_prerequisite:
        blocked_reason = _blocked_reason(failed_prerequisite['code'], failed_prerequisite['message'])

    return IngestPeriodRunSummary(
        period=YearMonthValue(str(period)),
        state=state,
        latest_attempt_at=_iso(latest.requested_at) if latest else None,
        latest_success_at=_iso(latest_success.finished_at) if latest_success else None,
        freshness_at=_iso(latest_success.finished_at) if latest_success else None,
        records=_records(latest),
        outputs=(latest.output_summary if latest else {}),
        validation=IngestValidation(
            state=validation_state,
            code=(latest.error_code if latest and latest.error_code else None),
            message=(latest.error_message if latest and latest.error_message else None),
        ),
        prerequisites=typed_prerequisites,
        blocked_reason=blocked_reason,
        next_valid_action=next_action,
    )


def build_ingest_overview(info, input_value) -> IngestOverview:
    request = info.context.request
    membership = require_entity_access(info, input_value.entity_id)
    periods = [str(period) for period in input_value.periods]
    if not 1 <= len(periods) <= 12 or len(set(periods)) != len(periods):
        raise GraphQLError(
            'periods must contain 1-12 unique YearMonth values.',
            extensions={
                'code': 'VALIDATION_ERROR',
                'correlationId': getattr(request, 'graphql_correlation_id', '-'),
            },
        )
    periods.sort()
    can_run = RUN_INGESTION_PROCESS_CAPABILITY in capability_codes_for_membership(membership)
    definitions = list(IngestSourceJobDefinition.objects.filter(
        entity=membership.entity,
        active=True,
    ).order_by('pk'))

    typed_definitions = []
    attention_count = 0
    completed_units = 0
    required_units = 0
    any_activity = False
    for definition in definitions:
        summaries = [
            _summary(definition, membership.entity, period, can_run)
            for period in periods
        ]
        typed_definitions.append(IngestSourceJobDefinitionType(
            id=strawberry.ID(str(definition.pk)),
            key=definition.key,
            source_family=IngestSourceFamily(definition.source_family),
            label=definition.label,
            required=definition.required,
            connection_state=_connection_state(definition, membership.entity),
            supported_operations=list(definition.supported_operations or []),
            read_capabilities=list(definition.read_capabilities or []),
            period_run_summaries=summaries,
        ))
        attention_states = {
            IngestPeriodRunState.NOT_CONFIGURED,
            IngestPeriodRunState.TEMPORARILY_UNAVAILABLE,
            IngestPeriodRunState.FAILED,
            IngestPeriodRunState.BLOCKED,
        }
        if any(summary.state in attention_states for summary in summaries) or (
            definition.required and any(summary.state in {
                IngestPeriodRunState.NOT_RUN,
                IngestPeriodRunState.NO_PERIOD_DATA,
            } for summary in summaries)
        ):
            attention_count += 1
        if definition.required:
            required_units += len(summaries)
            completed_units += sum(
                summary.state == IngestPeriodRunState.SUCCEEDED
                and summary.validation.state == IngestValidationState.PASSED
                and summary.blocked_reason is None
                for summary in summaries
            )
        any_activity = any_activity or any(
            summary.state not in {IngestPeriodRunState.NOT_RUN, IngestPeriodRunState.NO_PERIOD_DATA}
            for summary in summaries
        )

    completion_percent = round((completed_units / required_units) * 100) if required_units else 0
    if required_units and completion_percent == 100 and attention_count == 0:
        stage_state = IngestStageState.COMPLETE
    elif attention_count:
        stage_state = IngestStageState.ATTENTION_REQUIRED
    elif any_activity:
        stage_state = IngestStageState.IN_PROGRESS
    else:
        stage_state = IngestStageState.NOT_STARTED

    return IngestOverview(
        entity_id=strawberry.ID(str(membership.entity_id)),
        selected_periods=[YearMonthValue(period) for period in periods],
        source_job_definitions=typed_definitions,
        attention_count=attention_count,
        stage_state=stage_state,
        completion_percent=completion_percent,
    )
