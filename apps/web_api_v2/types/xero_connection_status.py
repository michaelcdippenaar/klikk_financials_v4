import datetime
from enum import Enum
from typing import Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext


@strawberry.enum(name='AuthorizationState')
class XeroConnectionAuthorizationState(Enum):
    AUTHORIZED = 'AUTHORIZED'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    REAUTHORIZATION_REQUIRED = 'REAUTHORIZATION_REQUIRED'
    UNAVAILABLE = 'UNAVAILABLE'


@strawberry.enum(name='AvailabilityCode')
class XeroConnectionAvailabilityCode(Enum):
    AVAILABLE = 'AVAILABLE'
    NOT_CONFIGURED = 'NOT_CONFIGURED'
    UNAVAILABLE = 'UNAVAILABLE'
    PERMISSION_DENIED = 'PERMISSION_DENIED'
    ERROR = 'ERROR'


@strawberry.enum
class XeroConnectionActionKind(Enum):
    MANAGE_CONNECTION = 'MANAGE_CONNECTION'
    SYNC = 'SYNC'


@strawberry.type(name='ActionDescriptor')
class XeroConnectionActionDescriptor:
    kind: XeroConnectionActionKind
    permitted: bool
    reason: str
    required_capability: Optional[str]
    expected_state: Optional[str]


@strawberry.type
class XeroConnectionStatus:
    resolved_context: ResolvedFinancialContext
    configured: bool
    authorization_state: XeroConnectionAuthorizationState
    token_action_required: bool
    source_evidence_at: Optional[datetime.datetime]
    last_successful_run_at: Optional[datetime.datetime]
    availability_code: XeroConnectionAvailabilityCode
    user_safe_reason: Optional[str]
    actions: list[XeroConnectionActionDescriptor]
