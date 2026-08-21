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
SAFE_RUN_MESSAGES = {
    "PROCESS_FAILED": "The process run did not complete.",
    "PROCESS_BLOCKED": "The process run is blocked by a prerequisite.",
    "RUN_LEASE_EXPIRED": "The process run stopped before completion.",
    "XERO_REAUTHORIZATION_REQUIRED": "The Xero connection must be restored.",
    "XERO_DAILY_LIMIT_REACHED": "The Xero daily limit has been reached.",
    "DURABLE_WORKER_REQUIRED": "This process requires a durable background worker.",
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


def _latest_success(entity, process_key, period):
    return IngestProcessRun.objects.filter(
        entity=entity,
        process_key=process_key,
        state=IngestProcessRun.State.SUCCEEDED,
        run_periods__period=str(period),
    ).order_by('-finished_at', '-id').first()


def _numeric_value(values, *keys):
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def _records(run):
    values = run.records_summary if run and isinstance(run.records_summary, dict) else {}
    return IngestRecordsSummary(
        read=_numeric_value(values, 'read', 'records', 'processed'),
        created=_numeric_value(values, 'created'),
        updated=_numeric_value(values, 'updated'),
        skipped=_numeric_value(values, 'skipped'),
        failed=_numeric_value(values, 'failed', 'errors'),
    )

def _safe_error_values(run):
    if run is None or run.state not in {IngestProcessRun.State.FAILED, IngestProcessRun.State.BLOCKED}:
        return None, None
    fallback = "PROCESS_BLOCKED" if run.state == IngestProcessRun.State.BLOCKED else "PROCESS_FAILED"
    code = run.error_code if run.error_code in SAFE_RUN_MESSAGES else fallback
    return code, SAFE_RUN_MESSAGES[code]


def _safe_outputs(run):
    if run is None:
        return {}
    records = _records(run)
    fields = ("read", "created", "updated", "skipped", "failed")
    return {field: getattr(records, field) for field in fields if getattr(records, field) is not None}


def _blocked_reason(code, message):
    return IngestBlockedReason(code=code, message=message)


def _summary(definition, entity, period, can_run):
    connection_state = _connection_state(definition, entity)
    latest_any = IngestProcessRun.objects.filter(
        entity=entity,
        process_key=definition.key,
    ).order_by('-requested_at', '-id').first()
    latest = IngestProcessRun.objects.filter(
        entity=entity,
        process_key=definition.key,
        run_periods__period=str(period),
    ).order_by('-requested_at', '-id').first()
    latest_success = _latest_success(entity, definition.key, period)
    prerequisites = prerequisite_status(entity, definition.key)
    typed_prerequisites = [IngestPrerequisite(**item) for item in prerequisites]
    failed_prerequisite = next((item for item in prerequisites if not item['satisfied']), None)
    blocked_reason = None

    if connection_state == IngestConnectionState.NOT_CONFIGURED:
        state = IngestPeriodRunState.NOT_CONFIGURED
        validation_state = IngestValidationState.UNAVAILABLE
        next_action = IngestNextAction.CONFIGURE
        blocked_reason = _blocked_reason('NOT_CONFIGURED', 'This source is not configured.')
    elif connection_state == IngestConnectionState.TEMPORARILY_UNAVAILABLE:
        state = IngestPeriodRunState.TEMPORARILY_UNAVAILABLE
        validation_state = IngestValidationState.UNAVAILABLE
        next_action = IngestNextAction.REAUTHORIZE
        blocked_reason = _blocked_reason(
            'XERO_REAUTHORIZATION_REQUIRED',
            'The Xero connection must be re-authorized.',
        )
    elif latest is None:
        state = (
            IngestPeriodRunState.NO_PERIOD_DATA
            if latest_any is not None
            else IngestPeriodRunState.NOT_RUN
        )
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
        if latest.state == IngestProcessRun.State.BLOCKED:
            safe_code, safe_message = _safe_error_values(latest)
            blocked_reason = _blocked_reason(safe_code, safe_message)

    if blocked_reason is None and failed_prerequisite:
        blocked_reason = _blocked_reason(failed_prerequisite['code'], failed_prerequisite['message'])

    evidence_run = latest if connection_state == IngestConnectionState.CONFIGURED else None
    evidence_success = latest_success if connection_state == IngestConnectionState.CONFIGURED else None
    safe_code, safe_message = _safe_error_values(evidence_run)
    return IngestPeriodRunSummary(
        period=YearMonthValue(str(period)),
        state=state,
        latest_attempt_at=_iso(evidence_run.requested_at) if evidence_run else None,
        latest_success_at=_iso(evidence_success.finished_at) if evidence_success else None,
        freshness_at=_iso(evidence_success.finished_at) if evidence_success else None,
        records=_records(evidence_run),
        outputs=_safe_outputs(evidence_run),
        validation=IngestValidation(
            state=validation_state,
            code=safe_code,
            message=safe_message,
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
