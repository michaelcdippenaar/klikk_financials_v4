from apps.web_api_v2.services.entity_access import (
    RUN_INGESTION_PROCESS_CAPABILITY,
    capability_codes_for_membership,
)
from apps.web_api_v2.services.fiscal_context import resolve_financial_context
from apps.web_api_v2.services.investec_bank_status import read_investec_bank_status
from apps.web_api_v2.types.ingest import (
    IngestNextAction,
    IngestPeriodRunState,
    IngestRecordsSummary,
    IngestStageState,
    IngestValidation,
    IngestValidationState,
)
from apps.web_api_v2.types.overview_ingest import (
    OverviewIngestSourceKey,
    OverviewIngestSourceSummary,
    OverviewIngestSources,
    OverviewSourceAction,
    OverviewSourceActionKind,
    OverviewSourceAvailabilityCode,
    OverviewSourceMode,
    OverviewSourceState,
)
from apps.web_api_v2.queries.xero_pipeline import (
    PROCESS_BY_STAGE,
    build_xero_pipeline_summary,
)


UNAVAILABLE_SOURCES = (
    (
        OverviewIngestSourceKey.INVESTMENT_HOLDINGS,
        'Investment holdings',
        'Investec Wealth',
        OverviewSourceMode.MANUAL_UPLOAD,
        OverviewSourceAvailabilityCode.ENTITY_BINDING_REQUIRED,
        'Investment holdings are not yet linked to this entity.',
        (OverviewSourceActionKind.OPEN_WORKBENCH, OverviewSourceActionKind.UPLOAD),
    ),
    (
        OverviewIngestSourceKey.SHARE_TRANSACTIONS,
        'Share transactions',
        'Investec Wealth',
        OverviewSourceMode.MANUAL_UPLOAD,
        OverviewSourceAvailabilityCode.ENTITY_BINDING_REQUIRED,
        'Share transactions are not yet linked to this entity.',
        (OverviewSourceActionKind.OPEN_WORKBENCH, OverviewSourceActionKind.UPLOAD),
    ),
    (
        OverviewIngestSourceKey.WHATSAPP_RECEIPTS,
        'WhatsApp receipts',
        'WhatsApp',
        OverviewSourceMode.NOT_CONNECTED,
        OverviewSourceAvailabilityCode.ENTITY_BINDING_REQUIRED,
        'WhatsApp receipts are not yet linked to this entity.',
        (OverviewSourceActionKind.OPEN_WORKBENCH, OverviewSourceActionKind.CONFIGURE),
    ),
    (
        OverviewIngestSourceKey.EMAIL_DOCUMENTS,
        'Email documents',
        'Email',
        OverviewSourceMode.NOT_CONNECTED,
        OverviewSourceAvailabilityCode.NOT_IMPLEMENTED,
        'Email document ingestion is not available in this version.',
        (OverviewSourceActionKind.OPEN_WORKBENCH, OverviewSourceActionKind.CONFIGURE),
    ),
    (
        OverviewIngestSourceKey.MANUAL_DOCUMENT_UPLOADS,
        'Manual document uploads',
        'Klikk',
        OverviewSourceMode.ON_DEMAND,
        OverviewSourceAvailabilityCode.NOT_IMPLEMENTED,
        'Manual document ingestion is not available in this version.',
        (OverviewSourceActionKind.OPEN_WORKBENCH, OverviewSourceActionKind.UPLOAD),
    ),
    (
        OverviewIngestSourceKey.PLANNING_ANALYTICS_TARGETS,
        'Planning Analytics targets',
        'Planning Analytics',
        OverviewSourceMode.READ_ONLY,
        OverviewSourceAvailabilityCode.DURABLE_STATUS_REQUIRED,
        'Planning Analytics source status is not yet available for this entity.',
        (OverviewSourceActionKind.OPEN_WORKBENCH, OverviewSourceActionKind.REFRESH_STATUS),
    ),
)


def _disabled_actions(kinds, code, reason):
    return [
        OverviewSourceAction(
            kind=kind,
            permitted=False,
            process_key=None,
            required_capability=None,
            disabled_code=code,
            disabled_reason=reason,
        )
        for kind in kinds
    ]


def _investec_bank_source(resolved):
    status = read_investec_bank_status(
        resolved.entity_id,
        resolved.selected_periods,
    )
    if status is None:
        availability = OverviewSourceAvailabilityCode.ENTITY_BINDING_REQUIRED
        reason = 'Investec bank data is not yet linked to this entity.'
        state = OverviewSourceState.UNAVAILABLE
        records = None
        freshness_at = None
        validation = IngestValidation(
            state=IngestValidationState.UNAVAILABLE,
            code=None,
            message=None,
        )
    elif not status['configured']:
        availability = OverviewSourceAvailabilityCode.NOT_CONFIGURED
        reason = 'No Investec bank accounts are stored for this entity.'
        state = OverviewSourceState.UNAVAILABLE
        records = None
        freshness_at = None
        validation = IngestValidation(
            state=IngestValidationState.UNAVAILABLE,
            code=None,
            message=None,
        )
    else:
        availability = OverviewSourceAvailabilityCode.AVAILABLE
        reason = None
        state = OverviewSourceState.READY
        records = IngestRecordsSummary(read=status['records_read'])
        freshness_at = status['freshness_at']
        validation = IngestValidation(
            state=IngestValidationState.NOT_RUN,
            code=None,
            message=None,
        )

    action_reason = (
        reason
        or 'Investec bank status is read-only; live controls are not available.'
    )
    return OverviewIngestSourceSummary(
        key=OverviewIngestSourceKey.INVESTEC_BANK,
        label='Investec bank transactions',
        provider='Investec Bank',
        mode=OverviewSourceMode.AUTOMATIC,
        availability_code=availability,
        state=state,
        user_safe_reason=reason,
        remediation=None,
        latest_attempt_at=None,
        latest_success_at=None,
        freshness_at=freshness_at,
        records=records,
        outputs=[],
        validation=validation,
        actions=_disabled_actions(
            (
                OverviewSourceActionKind.OPEN_WORKBENCH,
                OverviewSourceActionKind.REFRESH_STATUS,
                OverviewSourceActionKind.VIEW_LAST_RUN,
            ),
            (
                'READ_ONLY_STATUS'
                if availability == OverviewSourceAvailabilityCode.AVAILABLE
                else availability.value
            ),
            action_reason,
        ),
        xero_pipeline_available=False,
    )


def _xero_availability(pipeline):
    states = {stage.state for stage in pipeline.stages}
    if IngestPeriodRunState.TEMPORARILY_UNAVAILABLE in states:
        return (
            OverviewSourceAvailabilityCode.TEMPORARILY_UNAVAILABLE,
            'The Xero connection is temporarily unavailable.',
            'Ask an administrator to restore the Xero connection.',
        )
    if states == {IngestPeriodRunState.NOT_CONFIGURED}:
        return (
            OverviewSourceAvailabilityCode.NOT_CONFIGURED,
            'Xero is not connected for this entity.',
            None,
        )
    return OverviewSourceAvailabilityCode.AVAILABLE, None, None


def _xero_state(pipeline, availability):
    if availability != OverviewSourceAvailabilityCode.AVAILABLE:
        return OverviewSourceState.UNAVAILABLE
    if pipeline.state == IngestStageState.COMPLETE:
        return OverviewSourceState.CURRENT
    if pipeline.state == IngestStageState.ATTENTION_REQUIRED:
        return OverviewSourceState.NEEDS_ATTENTION
    if pipeline.state == IngestStageState.IN_PROGRESS:
        return OverviewSourceState.IN_PROGRESS
    return OverviewSourceState.NOT_RUN


def _xero_validation(pipeline, availability):
    if availability != OverviewSourceAvailabilityCode.AVAILABLE:
        return IngestValidation(state=IngestValidationState.UNAVAILABLE, code=None, message=None)
    if pipeline.state == IngestStageState.COMPLETE:
        return IngestValidation(state=IngestValidationState.PASSED, code=None, message=None)
    if any(
        stage.validation.state == IngestValidationState.FAILED
        for stage in pipeline.stages
    ):
        return IngestValidation(
            state=IngestValidationState.FAILED,
            code='PIPELINE_REQUIRES_ATTENTION',
            message='One or more required Xero stages require attention.',
        )
    return IngestValidation(state=IngestValidationState.NOT_RUN, code=None, message=None)


def _xero_actions(pipeline, can_run):
    actions = [
        OverviewSourceAction(
            kind=OverviewSourceActionKind.OPEN_WORKBENCH,
            permitted=True,
            process_key=None,
            required_capability=None,
            disabled_code=None,
            disabled_reason=None,
        ),
        OverviewSourceAction(
            kind=OverviewSourceActionKind.VIEW_LAST_RUN,
            permitted=True,
            process_key=None,
            required_capability=None,
            disabled_code=None,
            disabled_reason=None,
        ),
    ]
    candidate = next(
        (
            stage
            for stage in pipeline.stages
            if stage.required and stage.state != IngestPeriodRunState.SUCCEEDED
        ),
        pipeline.stages[0],
    )
    action_kind = (
        OverviewSourceActionKind.RETRY
        if candidate.next_valid_action == IngestNextAction.RETRY
        else OverviewSourceActionKind.RUN
    )
    permitted = can_run and candidate.next_valid_action in {
        IngestNextAction.RUN,
        IngestNextAction.RETRY,
    }
    actions.append(OverviewSourceAction(
        kind=action_kind,
        permitted=permitted,
        process_key=PROCESS_BY_STAGE[candidate.key],
        required_capability=RUN_INGESTION_PROCESS_CAPABILITY,
        disabled_code=None if permitted else 'CAPABILITY_OR_PREREQUISITE_REQUIRED',
        disabled_reason=(
            None
            if permitted
            else 'An explicit execution grant and satisfied prerequisites are required.'
        ),
    ))
    return actions


def build_overview_ingest_sources(info, context_input):
    membership, resolved = resolve_financial_context(info, context_input)
    pipeline = build_xero_pipeline_summary(info, context_input)
    capabilities = capability_codes_for_membership(membership)
    can_run = RUN_INGESTION_PROCESS_CAPABILITY in capabilities
    availability, reason, remediation = _xero_availability(pipeline)
    xero_state = _xero_state(pipeline, availability)
    trial_balance = next(
        stage
        for stage in pipeline.stages
        if stage.key.name == 'TRIAL_BALANCE'
    )
    if availability == OverviewSourceAvailabilityCode.AVAILABLE:
        records = trial_balance.records
        outputs = trial_balance.outputs
        latest_attempt_at = max(
            (stage.latest_attempt_at for stage in pipeline.stages if stage.latest_attempt_at),
            default=None,
        )
        latest_success_at = max(
            (stage.latest_success_at for stage in pipeline.stages if stage.latest_success_at),
            default=None,
        )
    else:
        records = None
        outputs = []
        latest_attempt_at = None
        latest_success_at = None

    xero = OverviewIngestSourceSummary(
        key=OverviewIngestSourceKey.XERO,
        label='Xero ledger and documents',
        provider='Xero',
        mode=OverviewSourceMode.AUTOMATIC,
        availability_code=availability,
        state=xero_state,
        user_safe_reason=reason,
        remediation=remediation,
        latest_attempt_at=latest_attempt_at,
        latest_success_at=latest_success_at,
        freshness_at=latest_success_at,
        records=records,
        outputs=outputs,
        validation=_xero_validation(pipeline, availability),
        actions=_xero_actions(pipeline, can_run),
        xero_pipeline_available=True,
    )
    investec_bank = _investec_bank_source(resolved)
    unavailable = [
        OverviewIngestSourceSummary(
            key=key,
            label=label,
            provider=provider,
            mode=mode,
            availability_code=availability_code,
            state=OverviewSourceState.UNAVAILABLE,
            user_safe_reason=user_safe_reason,
            remediation=None,
            latest_attempt_at=None,
            latest_success_at=None,
            freshness_at=None,
            records=None,
            outputs=[],
            validation=IngestValidation(
                state=IngestValidationState.UNAVAILABLE,
                code=None,
                message=None,
            ),
            actions=_disabled_actions(
                action_kinds,
                availability_code.value,
                user_safe_reason,
            ),
            xero_pipeline_available=False,
        )
        for (
            key,
            label,
            provider,
            mode,
            availability_code,
            user_safe_reason,
            action_kinds,
        ) in UNAVAILABLE_SOURCES
    ]
    sources = [xero, investec_bank, *unavailable]
    actionable_attention_count = sum(
        source.state == OverviewSourceState.NEEDS_ATTENTION
        and any(action.permitted for action in source.actions)
        for source in sources
    )
    return OverviewIngestSources(
        context=resolved,
        sources=sources,
        actionable_attention_count=actionable_attention_count,
        live_source_count=sum(
            source.availability_code == OverviewSourceAvailabilityCode.AVAILABLE
            for source in sources
        ),
        unavailable_source_count=sum(
            source.availability_code != OverviewSourceAvailabilityCode.AVAILABLE
            for source in sources
        ),
    )
