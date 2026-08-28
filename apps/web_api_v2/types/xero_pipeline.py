import datetime
from enum import Enum
from typing import Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext
from apps.web_api_v2.types.ingest import (
    IngestBlockedReason,
    IngestNextAction,
    IngestPeriodRunState,
    IngestPrerequisite,
    IngestRecordsSummary,
    IngestStageState,
    IngestValidation,
    YearMonth,
)


@strawberry.enum
class XeroPipelineStageKey(Enum):
    METADATA = 'METADATA'
    TRANSACTIONS_JOURNALS = 'TRANSACTIONS_JOURNALS'
    INVOICES = 'INVOICES'
    PROCESS_JOURNAL_LINES = 'PROCESS_JOURNAL_LINES'
    TRIAL_BALANCE = 'TRIAL_BALANCE'
    DOCUMENTS = 'DOCUMENTS'
    AGED_PAYABLES = 'AGED_PAYABLES'
    AGED_RECEIVABLES = 'AGED_RECEIVABLES'


@strawberry.enum
class XeroPipelineStageCategory(Enum):
    REQUIRED = 'REQUIRED'
    SUPPORTING = 'SUPPORTING'


@strawberry.enum
class XeroPipelineOutputCode(Enum):
    READ = 'READ'
    CREATED = 'CREATED'
    UPDATED = 'UPDATED'
    SKIPPED = 'SKIPPED'
    FAILED = 'FAILED'


@strawberry.type
class XeroPipelineOutput:
    code: XeroPipelineOutputCode
    label: str
    count: int


@strawberry.type
class XeroPipelineBlocker:
    stage: XeroPipelineStageKey
    code: str
    user_safe_reason: str
    remediation: Optional[str]


@strawberry.type
class XeroPipelinePeriodRunSummary:
    period: YearMonth
    state: IngestPeriodRunState
    latest_run_id: Optional[strawberry.ID]
    latest_attempt_at: Optional[datetime.datetime]
    latest_success_at: Optional[datetime.datetime]
    freshness_at: Optional[datetime.datetime]
    records: Optional[IngestRecordsSummary]
    outputs: list[XeroPipelineOutput]
    validation: IngestValidation
    prerequisites: list[IngestPrerequisite]
    blocked_reason: Optional[IngestBlockedReason]
    next_valid_action: IngestNextAction


@strawberry.enum
class XeroSourceEvidenceState(Enum):
    """Whether the entity holds source data for this stage."""

    PRESENT = 'PRESENT'
    ABSENT = 'ABSENT'
    UNAVAILABLE = 'UNAVAILABLE'


@strawberry.type
class XeroSourceEvidence:
    """What data the entity holds, as distinct from what V2 has run.

    The 2026-08-22 postmortem requires these stay in separate typed fields:
    a source count must never be read as a successful V2 run. A measured zero
    is PRESENT with recordCount 0; not knowing is UNAVAILABLE.
    """

    state: XeroSourceEvidenceState
    label: str
    # False means the count is entity-wide reference data with no period at
    # all — an entity has accounts, not accounts-for-March.
    period_scoped: bool
    record_count: Optional[int]
    latest_record_at: Optional[datetime.datetime]
    user_safe_reason: Optional[str]


@strawberry.type
class XeroPipelineStageSummary:
    key: XeroPipelineStageKey
    process_key: str
    label: str
    order: int
    category: XeroPipelineStageCategory
    required: bool
    state: IngestPeriodRunState
    latest_run_id: Optional[strawberry.ID]
    latest_attempt_at: Optional[datetime.datetime]
    latest_success_at: Optional[datetime.datetime]
    freshness_at: Optional[datetime.datetime]
    records: Optional[IngestRecordsSummary]
    outputs: list[XeroPipelineOutput]
    validation: IngestValidation
    prerequisites: list[IngestPrerequisite]
    blocker: Optional[XeroPipelineBlocker]
    next_valid_action: IngestNextAction
    period_run_summaries: list[XeroPipelinePeriodRunSummary]
    source_evidence: Optional[XeroSourceEvidence]


@strawberry.type
class XeroPipelineSummary:
    context: ResolvedFinancialContext
    state: IngestStageState
    completion_percent: int
    attention_count: int
    first_blocker: Optional[XeroPipelineBlocker]
    stages: list[XeroPipelineStageSummary]


@strawberry.type
class XeroPipelineSafeError:
    code: str
    user_safe_reason: str
    correlation_id: str
    remediation: Optional[str]


@strawberry.type
class XeroPipelineRunSummary:
    id: strawberry.ID
    stage: XeroPipelineStageKey
    process_key: str
    state: IngestPeriodRunState
    requested_at: datetime.datetime
    started_at: Optional[datetime.datetime]
    finished_at: Optional[datetime.datetime]
    periods: list[YearMonth]


@strawberry.type
class XeroPipelineRunHistory:
    context: ResolvedFinancialContext
    stage: XeroPipelineStageKey
    runs: list[XeroPipelineRunSummary]


@strawberry.type
class XeroPipelineRunDetail:
    context: ResolvedFinancialContext
    id: strawberry.ID
    stage: XeroPipelineStageKey
    process_key: str
    state: IngestPeriodRunState
    requested_at: datetime.datetime
    started_at: Optional[datetime.datetime]
    finished_at: Optional[datetime.datetime]
    periods: list[YearMonth]
    records: Optional[IngestRecordsSummary]
    outputs: list[XeroPipelineOutput]
    validation: IngestValidation
    retryable: bool
    safe_error: Optional[XeroPipelineSafeError]
