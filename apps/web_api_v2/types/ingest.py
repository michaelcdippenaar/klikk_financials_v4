import datetime
from enum import Enum
from typing import NewType, Optional

import strawberry
from strawberry.scalars import JSON


def _parse_year_month(value):
    value = str(value)
    if (
        len(value) != 7
        or value[4] != '-'
        or not value[:4].isdigit()
        or not value[5:].isdigit()
        or not 1 <= int(value[5:]) <= 12
    ):
        raise ValueError('YearMonth must use YYYY-MM.')
    return value


YearMonthValue = NewType('YearMonthValue', str)
YearMonth = strawberry.scalar(
    YearMonthValue,
    name='YearMonth',
    description='An ISO accounting month in YYYY-MM form.',
    serialize=str,
    parse_value=_parse_year_month,
)


@strawberry.enum
class IngestSourceFamily(Enum):
    XERO = 'XERO'
    INVESTEC = 'INVESTEC'
    MANUAL = 'MANUAL'
    PLANNING_ANALYTICS = 'PLANNING_ANALYTICS'


@strawberry.enum
class IngestConnectionState(Enum):
    CONFIGURED = 'CONFIGURED'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    TEMPORARILY_UNAVAILABLE = 'TEMPORARILY_UNAVAILABLE'


@strawberry.enum
class IngestPeriodRunState(Enum):
    NOT_RUN = 'NOT_RUN'
    NO_PERIOD_DATA = 'NO_PERIOD_DATA'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    TEMPORARILY_UNAVAILABLE = 'TEMPORARILY_UNAVAILABLE'
    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    BLOCKED = 'BLOCKED'


@strawberry.enum
class IngestValidationState(Enum):
    NOT_RUN = 'NOT_RUN'
    PASSED = 'PASSED'
    FAILED = 'FAILED'


@strawberry.enum
class IngestNextAction(Enum):
    NONE = 'NONE'
    RUN = 'RUN'
    RETRY = 'RETRY'
    WAIT = 'WAIT'
    CONFIGURE = 'CONFIGURE'
    REAUTHORIZE = 'REAUTHORIZE'


@strawberry.enum
class IngestStageState(Enum):
    NOT_STARTED = 'NOT_STARTED'
    IN_PROGRESS = 'IN_PROGRESS'
    ATTENTION_REQUIRED = 'ATTENTION_REQUIRED'
    COMPLETE = 'COMPLETE'


@strawberry.input
class IngestOverviewInput:
    entity_id: strawberry.ID
    periods: list[YearMonth]


@strawberry.type
class IngestRecordsSummary:
    read: Optional[int] = None
    created: Optional[int] = None
    updated: Optional[int] = None
    skipped: Optional[int] = None
    failed: Optional[int] = None


@strawberry.type
class IngestValidation:
    state: IngestValidationState
    code: Optional[str]
    message: Optional[str]


@strawberry.type
class IngestPrerequisite:
    code: str
    satisfied: bool
    message: str


@strawberry.type
class IngestBlockedReason:
    code: str
    message: str


@strawberry.type
class IngestPeriodRunSummary:
    period: YearMonth
    state: IngestPeriodRunState
    latest_attempt_at: Optional[datetime.datetime]
    latest_success_at: Optional[datetime.datetime]
    freshness_at: Optional[datetime.datetime]
    records: IngestRecordsSummary
    outputs: JSON
    validation: IngestValidation
    prerequisites: list[IngestPrerequisite]
    blocked_reason: Optional[IngestBlockedReason]
    next_valid_action: IngestNextAction


@strawberry.type
class IngestSourceJobDefinition:
    id: strawberry.ID
    key: str
    source_family: IngestSourceFamily
    label: str
    required: bool
    connection_state: IngestConnectionState
    supported_operations: list[str]
    read_capabilities: list[str]
    period_run_summaries: list[IngestPeriodRunSummary]


@strawberry.type
class IngestOverview:
    entity_id: strawberry.ID
    selected_periods: list[YearMonth]
    source_job_definitions: list[IngestSourceJobDefinition]
    attention_count: int
    stage_state: IngestStageState
    completion_percent: int
