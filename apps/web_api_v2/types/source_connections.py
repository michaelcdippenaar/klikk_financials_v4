import datetime
from enum import Enum
from typing import Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext
from apps.web_api_v2.types.ingest import IngestPeriodRunState, IngestValidationState


@strawberry.enum
class SourceConnectionKey(Enum):
    XERO = 'XERO'
    INVESTEC_SHARE_TRADING = 'INVESTEC_SHARE_TRADING'
    WHATSAPP_RECEIPTS = 'WHATSAPP_RECEIPTS'
    EMAIL_RECEIPTS = 'EMAIL_RECEIPTS'
    PLANNING_ANALYTICS = 'PLANNING_ANALYTICS'
    EXCEL_ADD_IN = 'EXCEL_ADD_IN'


@strawberry.enum
class SourceConnectionCategory(Enum):
    ACCOUNTING = 'ACCOUNTING'
    INVESTMENTS = 'INVESTMENTS'
    RECEIPTS = 'RECEIPTS'
    DESTINATION = 'DESTINATION'
    IDENTITY = 'IDENTITY'


@strawberry.enum
class SourceConnectionConfigurationState(Enum):
    CONFIGURED = 'CONFIGURED'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    UNAVAILABLE = 'UNAVAILABLE'
    ERROR = 'ERROR'


@strawberry.enum
class SourceConnectionReadinessState(Enum):
    READY = 'READY'
    EMPTY = 'EMPTY'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    UNAVAILABLE = 'UNAVAILABLE'
    PERMISSION_DENIED = 'PERMISSION_DENIED'
    STALE = 'STALE'
    ERROR = 'ERROR'


@strawberry.enum
class SourceConnectionAvailabilityCode(Enum):
    AVAILABLE = 'AVAILABLE'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    UNAVAILABLE = 'UNAVAILABLE'
    PERMISSION_DENIED = 'PERMISSION_DENIED'
    STALE = 'STALE'
    ERROR = 'ERROR'


@strawberry.enum
class SourceConnectionActionKind(Enum):
    MANAGE_CONNECTION = 'MANAGE_CONNECTION'
    SYNC = 'SYNC'
    OPEN_WORKBENCH = 'OPEN_WORKBENCH'
    CONFIGURE = 'CONFIGURE'
    SUBMIT = 'SUBMIT'
    CONFIGURE_IDENTITY = 'CONFIGURE_IDENTITY'


@strawberry.type
class SourceConnectionAction:
    kind: SourceConnectionActionKind
    permitted: bool
    reason: str
    required_capability: Optional[str]
    expected_state: Optional[str]


@strawberry.type
class SourceConnection:
    key: SourceConnectionKey
    display_name: str
    category: SourceConnectionCategory
    configuration_state: SourceConnectionConfigurationState
    readiness_state: SourceConnectionReadinessState
    availability_code: SourceConnectionAvailabilityCode
    user_safe_reason: Optional[str]
    source_evidence_count: Optional[int]
    source_evidence_at: Optional[datetime.datetime]
    last_successful_run_at: Optional[datetime.datetime]
    validation_state: IngestValidationState
    latest_v2_run_state: Optional[IngestPeriodRunState]
    safe_identity: Optional[str]
    actions: list[SourceConnectionAction]


@strawberry.type
class SourceConnectionsSummary:
    total: int
    active: int
    needs_setup: int
    ready_destinations: int


@strawberry.type
class SourceConnections:
    resolved_context: ResolvedFinancialContext
    checked_at: datetime.datetime
    summary: SourceConnectionsSummary
    connections: list[SourceConnection]
