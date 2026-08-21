import datetime
from enum import Enum
from typing import Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext
from apps.web_api_v2.types.ingest import IngestRecordsSummary, IngestValidation
from apps.web_api_v2.types.xero_pipeline import XeroPipelineOutput


@strawberry.enum
class OverviewIngestSourceKey(Enum):
    XERO = 'XERO'
    INVESTEC_BANK = 'INVESTEC_BANK'
    INVESTMENT_HOLDINGS = 'INVESTMENT_HOLDINGS'
    SHARE_TRANSACTIONS = 'SHARE_TRANSACTIONS'
    WHATSAPP_RECEIPTS = 'WHATSAPP_RECEIPTS'
    EMAIL_DOCUMENTS = 'EMAIL_DOCUMENTS'
    MANUAL_DOCUMENT_UPLOADS = 'MANUAL_DOCUMENT_UPLOADS'
    PLANNING_ANALYTICS_TARGETS = 'PLANNING_ANALYTICS_TARGETS'


@strawberry.enum
class OverviewSourceAvailabilityCode(Enum):
    AVAILABLE = 'AVAILABLE'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    NO_PERIOD_DATA = 'NO_PERIOD_DATA'
    TEMPORARILY_UNAVAILABLE = 'TEMPORARILY_UNAVAILABLE'
    ENTITY_BINDING_REQUIRED = 'ENTITY_BINDING_REQUIRED'
    DURABLE_STATUS_REQUIRED = 'DURABLE_STATUS_REQUIRED'
    NOT_IMPLEMENTED = 'NOT_IMPLEMENTED'


@strawberry.enum
class OverviewSourceState(Enum):
    CURRENT = 'CURRENT'
    NEEDS_ATTENTION = 'NEEDS_ATTENTION'
    IN_PROGRESS = 'IN_PROGRESS'
    READY = 'READY'
    NOT_RUN = 'NOT_RUN'
    UNAVAILABLE = 'UNAVAILABLE'


@strawberry.enum
class OverviewSourceMode(Enum):
    AUTOMATIC = 'AUTOMATIC'
    MANUAL_UPLOAD = 'MANUAL_UPLOAD'
    NOT_CONNECTED = 'NOT_CONNECTED'
    ON_DEMAND = 'ON_DEMAND'
    READ_ONLY = 'READ_ONLY'


@strawberry.enum
class OverviewSourceActionKind(Enum):
    OPEN_WORKBENCH = 'OPEN_WORKBENCH'
    VIEW_LAST_RUN = 'VIEW_LAST_RUN'
    RUN = 'RUN'
    RETRY = 'RETRY'
    CONFIGURE = 'CONFIGURE'
    UPLOAD = 'UPLOAD'
    REFRESH_STATUS = 'REFRESH_STATUS'


@strawberry.type
class OverviewSourceAction:
    kind: OverviewSourceActionKind
    permitted: bool
    process_key: Optional[str]
    required_capability: Optional[str]
    disabled_code: Optional[str]
    disabled_reason: Optional[str]


@strawberry.type
class OverviewIngestSourceSummary:
    key: OverviewIngestSourceKey
    label: str
    provider: str
    mode: OverviewSourceMode
    availability_code: OverviewSourceAvailabilityCode
    state: OverviewSourceState
    user_safe_reason: Optional[str]
    remediation: Optional[str]
    latest_attempt_at: Optional[datetime.datetime]
    latest_success_at: Optional[datetime.datetime]
    freshness_at: Optional[datetime.datetime]
    records: Optional[IngestRecordsSummary]
    outputs: list[XeroPipelineOutput]
    validation: IngestValidation
    actions: list[OverviewSourceAction]
    xero_pipeline_available: bool


@strawberry.type
class OverviewIngestSources:
    context: ResolvedFinancialContext
    sources: list[OverviewIngestSourceSummary]
    actionable_attention_count: int
    live_source_count: int
    unavailable_source_count: int
