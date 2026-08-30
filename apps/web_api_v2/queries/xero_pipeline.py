import uuid

import strawberry
from django.db import DatabaseError
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from graphql import GraphQLError

from apps.web_api_v2.models import (
    IngestProcessRun,
    IngestProcessRunPeriod,
    IngestSourceJobDefinition,
)
from apps.web_api_v2.services.entity_access import (
    RUN_INGESTION_PROCESS_CAPABILITY,
    VIEW_FINANCIALS_CAPABILITY,
    capability_codes_for_membership,
)
from apps.web_api_v2.services.fiscal_context import resolve_financial_context
from apps.web_api_v2.services.ingest_registry import has_tenant_credentials, prerequisite_status
from apps.web_api_v2.types.ingest import (
    IngestBlockedReason,
    IngestNextAction,
    IngestPeriodRunState,
    IngestPrerequisite,
    IngestRecordsSummary,
    IngestStageState,
    IngestValidation,
    IngestValidationState,
    YearMonthValue,
)
from apps.web_api_v2.services.xero_source_evidence import is_period_scoped, measure_stage_source
from apps.web_api_v2.types.xero_pipeline import (
    XeroPipelineBlocker,
    XeroPipelineOutput,
    XeroPipelineOutputCode,
    XeroPipelinePeriodRunSummary,
    XeroPipelineRunDetail,
    XeroPipelineRunHistory,
    XeroPipelineRunSummary,
    XeroPipelineSafeError,
    XeroPipelineStageCategory,
    XeroPipelineStageKey,
    XeroPipelineStageSummary,
    XeroSourceEvidence,
    XeroSourceEvidenceState,
    XeroPipelineSummary,
)


STAGES = (
    (XeroPipelineStageKey.METADATA, 'metadata', 'Metadata', True),
    (
        XeroPipelineStageKey.TRANSACTIONS_JOURNALS,
        'transaction-journal-sync',
        'Transactions/Journals',
        True,
    ),
    (XeroPipelineStageKey.INVOICES, 'invoice-sync', 'Invoices', True),
    (
        XeroPipelineStageKey.PROCESS_JOURNAL_LINES,
        'process-journals',
        'Process journal lines',
        True,
    ),
    (XeroPipelineStageKey.TRIAL_BALANCE, 'trail-balance', 'Trial Balance', True),
    (XeroPipelineStageKey.DOCUMENTS, 'documents', 'Documents', False),
    (XeroPipelineStageKey.AGED_PAYABLES, 'aged-payables', 'Aged Payables', False),
    (XeroPipelineStageKey.AGED_RECEIVABLES, 'aged-receivables', 'Aged Receivables', False),
)
PROCESS_BY_STAGE = {stage: process for stage, process, _label, _required in STAGES}
STAGE_BY_PROCESS = {process: stage for stage, process, _label, _required in STAGES}
RUN_STATE_MAP = {
    IngestProcessRun.State.QUEUED: IngestPeriodRunState.QUEUED,
    IngestProcessRun.State.RUNNING: IngestPeriodRunState.RUNNING,
    IngestProcessRun.State.SUCCEEDED: IngestPeriodRunState.SUCCEEDED,
    IngestProcessRun.State.FAILED: IngestPeriodRunState.FAILED,
    IngestProcessRun.State.CANCELLED: IngestPeriodRunState.CANCELLED,
    IngestProcessRun.State.BLOCKED: IngestPeriodRunState.BLOCKED,
}
STATE_PRIORITY = {
    IngestPeriodRunState.TEMPORARILY_UNAVAILABLE: 10,
    IngestPeriodRunState.NOT_CONFIGURED: 9,
    IngestPeriodRunState.FAILED: 8,
    IngestPeriodRunState.BLOCKED: 7,
    IngestPeriodRunState.CANCELLED: 6,
    IngestPeriodRunState.RUNNING: 5,
    IngestPeriodRunState.QUEUED: 4,
    IngestPeriodRunState.NO_PERIOD_DATA: 3,
    IngestPeriodRunState.NOT_RUN: 2,
    IngestPeriodRunState.SUCCEEDED: 1,
}
SAFE_ERROR_REASONS = {
    'PROCESS_FAILED': 'The process run did not complete.',
    'RUN_LEASE_EXPIRED': 'The process run stopped before completion.',
    'XERO_REAUTHORIZATION_REQUIRED': 'The Xero connection must be restored.',
    'XERO_DAILY_LIMIT_REACHED': 'The Xero daily limit has been reached.',
    'DURABLE_WORKER_REQUIRED': 'This process requires a durable background worker.',
}
SAFE_ERROR_REMEDIATION = {
    'RUN_LEASE_EXPIRED': 'Retry the stage when execution is available.',
    'XERO_REAUTHORIZATION_REQUIRED': 'Ask an administrator to restore the Xero connection.',
    'XERO_DAILY_LIMIT_REACHED': 'Retry after the Xero limit resets.',
}
OUTPUT_FIELDS = (
    ('read', XeroPipelineOutputCode.READ, 'Records read'),
    ('created', XeroPipelineOutputCode.CREATED, 'Records created'),
    ('updated', XeroPipelineOutputCode.UPDATED, 'Records updated'),
    ('skipped', XeroPipelineOutputCode.SKIPPED, 'Records skipped'),
    ('failed', XeroPipelineOutputCode.FAILED, 'Records failed'),
)


def _graphql_error(info, code, message, *, retryable=False):
    raise GraphQLError(
        message,
        extensions={
            'code': code,
            'correlationId': getattr(info.context.request, 'graphql_correlation_id', '-'),
            'retryable': retryable,
        },
    )


def _require_view(membership, info):
    if VIEW_FINANCIALS_CAPABILITY not in capability_codes_for_membership(membership):
        _graphql_error(
            info,
            'CAPABILITY_REQUIRED',
            'VIEW_FINANCIALS capability is required.',
        )


def _safe_count(values, *keys):
    if not isinstance(values, dict):
        return None
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def records_for_run(run):
    if run is None:
        return None
    values = run.records_summary
    return IngestRecordsSummary(
        read=_safe_count(values, 'read', 'records', 'processed'),
        created=_safe_count(values, 'created'),
        updated=_safe_count(values, 'updated'),
        skipped=_safe_count(values, 'skipped'),
        failed=_safe_count(values, 'failed', 'errors'),
    )


def outputs_for_records(records):
    if records is None:
        return []
    outputs = []
    for field, code, label in OUTPUT_FIELDS:
        value = getattr(records, field)
        if value is not None:
            outputs.append(XeroPipelineOutput(code=code, label=label, count=value))
    return outputs


def _safe_error(run):
    if run is None or not run.error_code:
        return None
    code = run.error_code if run.error_code in SAFE_ERROR_REASONS else 'PROCESS_FAILED'
    return XeroPipelineSafeError(
        code=code,
        user_safe_reason=SAFE_ERROR_REASONS[code],
        correlation_id=str(run.correlation_id),
        remediation=SAFE_ERROR_REMEDIATION.get(code),
    )


def _validation(run, state):
    if state in {
        IngestPeriodRunState.NOT_CONFIGURED,
        IngestPeriodRunState.TEMPORARILY_UNAVAILABLE,
    }:
        return IngestValidation(
            state=IngestValidationState.UNAVAILABLE,
            code=None,
            message=None,
        )
    if run is None or state in {
        IngestPeriodRunState.NOT_RUN,
        IngestPeriodRunState.NO_PERIOD_DATA,
        IngestPeriodRunState.QUEUED,
        IngestPeriodRunState.RUNNING,
        IngestPeriodRunState.CANCELLED,
    }:
        return IngestValidation(
            state=IngestValidationState.NOT_RUN,
            code=None,
            message=None,
        )
    if state == IngestPeriodRunState.SUCCEEDED:
        return IngestValidation(
            state=IngestValidationState.PASSED,
            code=None,
            message=None,
        )
    safe = _safe_error(run)
    return IngestValidation(
        state=IngestValidationState.FAILED,
        code=safe.code if safe else 'PROCESS_FAILED',
        message=safe.user_safe_reason if safe else 'The process run did not complete.',
    )


def _connection_state(entity, definition):
    if entity.reauth_required:
        return IngestPeriodRunState.TEMPORARILY_UNAVAILABLE
    if (
        definition is None
        or definition.configuration_state
        != IngestSourceJobDefinition.ConfigurationState.CONFIGURED
        or not has_tenant_credentials(entity.pk)
    ):
        return IngestPeriodRunState.NOT_CONFIGURED
    return None


def _safe_blocked_reason(run, failed_prerequisite):
    if failed_prerequisite:
        return IngestBlockedReason(
            code=failed_prerequisite['code'],
            message=failed_prerequisite['message'],
        )
    safe = _safe_error(run)
    if safe:
        return IngestBlockedReason(code=safe.code, message=safe.user_safe_reason)
    return None


def _next_action(state, run, can_run, failed_prerequisite):
    if state in {IngestPeriodRunState.QUEUED, IngestPeriodRunState.RUNNING}:
        return IngestNextAction.WAIT
    if not can_run or failed_prerequisite or state in {
        IngestPeriodRunState.NOT_CONFIGURED,
        IngestPeriodRunState.TEMPORARILY_UNAVAILABLE,
    }:
        return IngestNextAction.NONE
    if state in {IngestPeriodRunState.FAILED, IngestPeriodRunState.BLOCKED}:
        return IngestNextAction.RETRY if run and run.retryable else IngestNextAction.NONE
    return IngestNextAction.RUN


def _latest_period_maps(entity, periods):
    process_keys = list(STAGE_BY_PROCESS)
    base = IngestProcessRunPeriod.objects.filter(
        run__entity=entity,
        run__process_key__in=process_keys,
        period__in=periods,
    )
    ranking = {
        'partition_by': [F('run__process_key'), F('period')],
        'order_by': [F('run__requested_at').desc(), F('run_id').desc()],
    }
    latest_rows = base.annotate(
        row_number=Window(expression=RowNumber(), **ranking),
    ).filter(row_number=1).select_related('run')
    success_rows = base.filter(
        run__state=IngestProcessRun.State.SUCCEEDED,
    ).annotate(
        row_number=Window(expression=RowNumber(), **ranking),
    ).filter(row_number=1).select_related('run')
    latest = {(row.run.process_key, row.period): row.run for row in latest_rows}
    successes = {(row.run.process_key, row.period): row.run for row in success_rows}

    # A period-agnostic stage's runs carry no period rows at all, so the joins
    # above can never see them. Metadata succeeded twice on 30 Aug and the
    # workbench still read "Not run", because a run filed under no month is
    # invisible to a per-month lookup. These are the same runs, read without
    # the period scope, for the stages whose evidence was never about a month.
    unscoped_runs = IngestProcessRun.objects.filter(
        entity=entity,
        process_key__in=process_keys,
    ).order_by('-requested_at', '-pk')
    unscoped_latest = {}
    unscoped_successes = {}
    for run in unscoped_runs:
        unscoped_latest.setdefault(run.process_key, run)
        if run.state == IngestProcessRun.State.SUCCEEDED:
            unscoped_successes.setdefault(run.process_key, run)
    all_processes = set(IngestProcessRun.objects.filter(
        entity=entity,
        process_key__in=process_keys,
    ).values_list('process_key', flat=True).distinct())
    matched_ids = {run.pk for run in latest.values()} | {run.pk for run in successes.values()}
    run_periods = {}
    for run_id, period in IngestProcessRunPeriod.objects.filter(
        run_id__in=matched_ids,
    ).values_list('run_id', 'period'):
        run_periods.setdefault(run_id, set()).add(period)
    return latest, successes, all_processes, run_periods, unscoped_latest, unscoped_successes


def _period_summary(
    period,
    process_key,
    connection_state,
    latest,
    latest_success,
    process_has_any_run,
    run_periods,
    prerequisites,
    can_run,
):
    failed_prerequisite = next((item for item in prerequisites if not item['satisfied']), None)
    if connection_state is not None:
        state = connection_state
        run = None
    else:
        run = latest
        if run is None:
            state = (
                IngestPeriodRunState.NO_PERIOD_DATA
                if process_has_any_run
                else IngestPeriodRunState.NOT_RUN
            )
        else:
            state = RUN_STATE_MAP[run.state]

    records = records_for_run(run)
    if run is not None and len(run_periods.get(run.pk, ())) > 1:
        records = None
    outputs = outputs_for_records(records)
    blocked = _safe_blocked_reason(run, failed_prerequisite)
    return XeroPipelinePeriodRunSummary(
        period=YearMonthValue(period),
        state=state,
        latest_run_id=(strawberry.ID(str(run.pk)) if run else None),
        latest_attempt_at=run.requested_at if run else None,
        latest_success_at=latest_success.finished_at if latest_success else None,
        freshness_at=latest_success.finished_at if latest_success else None,
        records=records,
        outputs=outputs,
        validation=_validation(run, state),
        prerequisites=[IngestPrerequisite(**item) for item in prerequisites],
        blocked_reason=blocked,
        next_valid_action=_next_action(state, run, can_run, failed_prerequisite),
    ), run


def _sum_records(runs, run_periods, selected_periods):
    unique = {run.pk: run for run in runs if run is not None}
    if not unique:
        return None
    selected = set(selected_periods)
    scopes = []
    for run in unique.values():
        scope = set(run_periods.get(run.pk, ()))
        if not scope or not scope.issubset(selected):
            return None
        if any(scope & existing for existing in scopes):
            return None
        scopes.append(scope)
    records = [records_for_run(run) for run in unique.values()]
    values = {}
    for field, _code, _label in OUTPUT_FIELDS:
        measured = [getattr(item, field) for item in records if getattr(item, field) is not None]
        values[field] = sum(measured) if measured else None
    if all(value is None for value in values.values()):
        return None
    return IngestRecordsSummary(**values)


def _first_blocker_for_stage(stage_key, state, blocker):
    if state == IngestPeriodRunState.SUCCEEDED and blocker is None:
        return None
    if blocker:
        code = blocker.code
        reason = blocker.message
    else:
        code = state.value
        reason = {
            IngestPeriodRunState.NOT_RUN: 'This required Xero stage has not run.',
            IngestPeriodRunState.NO_PERIOD_DATA: 'No period-specific run evidence is available.',
            IngestPeriodRunState.NOT_CONFIGURED: 'Xero is not connected for this entity.',
            IngestPeriodRunState.TEMPORARILY_UNAVAILABLE: 'The Xero connection is temporarily unavailable.',
            IngestPeriodRunState.RUNNING: 'This Xero stage is still running.',
            IngestPeriodRunState.QUEUED: 'This Xero stage is waiting to run.',
            IngestPeriodRunState.FAILED: 'This Xero stage did not complete.',
            IngestPeriodRunState.BLOCKED: 'This Xero stage is blocked.',
            IngestPeriodRunState.CANCELLED: 'This Xero stage was cancelled.',
        }.get(state, 'This Xero stage requires attention.')
    remediation = (
        'Ask an administrator to restore the Xero connection.'
        if code == 'XERO_REAUTHORIZATION_REQUIRED'
        else None
    )
    return XeroPipelineBlocker(
        stage=stage_key,
        code=code,
        user_safe_reason=reason,
        remediation=remediation,
    )


def _source_evidence(stage_key, entity, starts_on, ends_on):
    """Source evidence for one stage, kept separate from run evidence.

    Aggregate-only: a count and a max per stage, so the whole summary stays at
    a bounded number of queries regardless of how many periods are selected.
    A DatabaseError becomes UNAVAILABLE rather than a fabricated zero — the
    difference between "you have none" and "we could not tell" is exactly what
    the 2026-08-22 postmortem was about.
    """
    try:
        measurement = measure_stage_source(
            stage_key.value, entity=entity, starts_on=starts_on, ends_on=ends_on,
        )
    except DatabaseError:
        return XeroSourceEvidence(
            state=XeroSourceEvidenceState.UNAVAILABLE,
            label='source records', period_scoped=False, record_count=None,
            latest_record_at=None,
            user_safe_reason='Source evidence is temporarily unavailable.',
        )
    if measurement is None:
        return None
    if measurement.count is None:
        return XeroSourceEvidence(
            state=XeroSourceEvidenceState.UNAVAILABLE,
            label=measurement.label, period_scoped=measurement.period_scoped,
            record_count=None, latest_record_at=None,
            user_safe_reason=measurement.reason,
        )
    return XeroSourceEvidence(
        state=(
            XeroSourceEvidenceState.PRESENT
            if measurement.count
            else XeroSourceEvidenceState.ABSENT
        ),
        label=measurement.label,
        period_scoped=measurement.period_scoped,
        record_count=measurement.count,
        latest_record_at=measurement.latest_at,
        user_safe_reason=(
            None if measurement.count
            else 'No source records were found for the selected scope.'
        ),
    )


def build_xero_pipeline_summary(info, context_input):
    membership, context = resolve_financial_context(info, context_input)
    _require_view(membership, info)
    periods = [str(period) for period in context.selected_periods]
    (
        latest, successes, process_has_runs, run_periods,
        unscoped_latest, unscoped_successes,
    ) = _latest_period_maps(
        membership.entity,
        periods,
    )
    capabilities = capability_codes_for_membership(membership)
    can_run = RUN_INGESTION_PROCESS_CAPABILITY in capabilities
    definitions = {
        row.key: row
        for row in IngestSourceJobDefinition.objects.filter(
            entity=membership.entity,
            active=True,
            key__in=list(STAGE_BY_PROCESS),
        )
    }

    stages = []
    first_blocker = None
    attention_count = 0
    completed_units = 0
    required_units = 0
    any_activity = False
    for order, (stage_key, process_key, label, required) in enumerate(STAGES, start=1):
        prerequisites = prerequisite_status(membership.entity, process_key)
        connection_state = _connection_state(membership.entity, definitions.get(process_key))
        summaries = []
        evidence_runs = []
        # A period-agnostic stage reports the same run for every selected
        # period, because its evidence was never about a month.
        period_scoped = is_period_scoped(stage_key.value)
        for period in periods:
            summary, run = _period_summary(
                period,
                process_key,
                connection_state,
                (latest.get((process_key, period)) if period_scoped
                 else unscoped_latest.get(process_key)),
                (successes.get((process_key, period)) if period_scoped
                 else unscoped_successes.get(process_key)),
                process_key in process_has_runs,
                run_periods,
                prerequisites,
                can_run,
            )
            summaries.append(summary)
            evidence_runs.append(run)

        state = max((item.state for item in summaries), key=STATE_PRIORITY.get)
        latest_run = max(
            (run for run in evidence_runs if run is not None),
            key=lambda run: (run.requested_at, str(run.pk)),
            default=None,
        )
        latest_attempt_at = max(
            (item.latest_attempt_at for item in summaries if item.latest_attempt_at),
            default=None,
        )
        latest_success_at = max(
            (item.latest_success_at for item in summaries if item.latest_success_at),
            default=None,
        )
        records = _sum_records(evidence_runs, run_periods, periods)
        validation = (
            IngestValidation(state=IngestValidationState.PASSED, code=None, message=None)
            if all(item.validation.state == IngestValidationState.PASSED for item in summaries)
            else IngestValidation(state=IngestValidationState.UNAVAILABLE, code=None, message=None)
            if any(item.validation.state == IngestValidationState.UNAVAILABLE for item in summaries)
            else IngestValidation(state=IngestValidationState.FAILED, code='VALIDATION_FAILED', message='A selected period requires attention.')
            if any(item.validation.state == IngestValidationState.FAILED for item in summaries)
            else IngestValidation(state=IngestValidationState.NOT_RUN, code=None, message=None)
        )
        blocker_reason = next((item.blocked_reason for item in summaries if item.blocked_reason), None)
        blocker = _first_blocker_for_stage(stage_key, state, blocker_reason)
        next_action = next(
            (
                item.next_valid_action
                for item in summaries
                if item.next_valid_action
                in {IngestNextAction.RETRY, IngestNextAction.RUN, IngestNextAction.WAIT}
            ),
            IngestNextAction.NONE,
        )
        stage = XeroPipelineStageSummary(
            key=stage_key,
            process_key=process_key,
            label=label,
            order=order,
            category=(
                XeroPipelineStageCategory.REQUIRED
                if required
                else XeroPipelineStageCategory.SUPPORTING
            ),
            required=required,
            state=state,
            latest_run_id=(strawberry.ID(str(latest_run.pk)) if latest_run else None),
            latest_attempt_at=latest_attempt_at,
            latest_success_at=latest_success_at,
            freshness_at=latest_success_at,
            records=records,
            outputs=outputs_for_records(records),
            validation=validation,
            prerequisites=[IngestPrerequisite(**item) for item in prerequisites],
            blocker=blocker,
            next_valid_action=next_action,
            period_run_summaries=summaries,
            source_evidence=_source_evidence(
                stage_key, membership.entity, context.starts_on, context.ends_on,
            ),
        )
        stages.append(stage)

        if required:
            required_units += len(summaries)
            completed_units += sum(
                item.state == IngestPeriodRunState.SUCCEEDED
                and item.validation.state == IngestValidationState.PASSED
                and item.blocked_reason is None
                for item in summaries
            )
            if first_blocker is None and blocker is not None:
                first_blocker = blocker
        if blocker is not None:
            attention_count += 1
        any_activity = any_activity or any(
            item.state not in {
                IngestPeriodRunState.NOT_RUN,
                IngestPeriodRunState.NO_PERIOD_DATA,
            }
            for item in summaries
        )

    completion_percent = round((completed_units / required_units) * 100) if required_units else 0
    if required_units and completion_percent == 100 and first_blocker is None:
        stage_state = IngestStageState.COMPLETE
    elif first_blocker is not None:
        stage_state = IngestStageState.ATTENTION_REQUIRED
    elif any_activity:
        stage_state = IngestStageState.IN_PROGRESS
    else:
        stage_state = IngestStageState.NOT_STARTED
    return XeroPipelineSummary(
        context=context,
        state=stage_state,
        completion_percent=completion_percent,
        attention_count=attention_count,
        first_blocker=first_blocker,
        stages=stages,
    )


def _run_summary(run, stage, selected_periods):
    selected = set(selected_periods)
    periods = sorted(
        period
        for period in run.run_periods.values_list('period', flat=True)
        if period in selected
    )
    return XeroPipelineRunSummary(
        id=strawberry.ID(str(run.pk)),
        stage=stage,
        process_key=run.process_key,
        state=RUN_STATE_MAP[run.state],
        requested_at=run.requested_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        periods=[YearMonthValue(period) for period in periods],
    )


def build_xero_pipeline_run_history(info, context_input, stage, limit):
    membership, context = resolve_financial_context(info, context_input)
    _require_view(membership, info)
    if not 1 <= limit <= 20:
        _graphql_error(info, 'VALIDATION_ERROR', 'limit must be between 1 and 20.')
    process_key = PROCESS_BY_STAGE[stage]
    selected_periods = [str(period) for period in context.selected_periods]
    runs = list(IngestProcessRun.objects.filter(
        entity=membership.entity,
        process_key=process_key,
        run_periods__period__in=selected_periods,
    ).distinct().prefetch_related('run_periods').order_by('-requested_at', '-id')[:limit])
    return XeroPipelineRunHistory(
        context=context,
        stage=stage,
        runs=[_run_summary(run, stage, selected_periods) for run in runs],
    )


def build_xero_pipeline_run_detail(info, context_input, run_id):
    membership, context = resolve_financial_context(info, context_input)
    _require_view(membership, info)
    try:
        parsed_id = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        _graphql_error(info, 'VALIDATION_ERROR', 'runId is not valid.')
    selected_periods = [str(period) for period in context.selected_periods]
    run = IngestProcessRun.objects.filter(
        pk=parsed_id,
        entity=membership.entity,
        process_key__in=list(STAGE_BY_PROCESS),
        run_periods__period__in=selected_periods,
    ).distinct().prefetch_related('run_periods').first()
    if run is None:
        _graphql_error(info, 'RUN_NOT_FOUND', 'Process run was not found.')
    stage = STAGE_BY_PROCESS[run.process_key]
    summary = _run_summary(run, stage, selected_periods)
    records = records_for_run(run)
    return XeroPipelineRunDetail(
        context=context,
        id=summary.id,
        stage=stage,
        process_key=run.process_key,
        state=summary.state,
        requested_at=summary.requested_at,
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        periods=summary.periods,
        records=records,
        outputs=outputs_for_records(records),
        validation=_validation(run, summary.state),
        retryable=run.retryable,
        safe_error=_safe_error(run),
    )
