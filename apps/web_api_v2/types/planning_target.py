import datetime
from enum import Enum
from typing import Optional

import strawberry

from apps.web_api_v2.types.financial_context import ResolvedFinancialContext


@strawberry.enum
class PlanningTargetState(Enum):
    """Bound, approved and ready are three different things.

    NOT_BOUND    nobody has said where this entity's data should go
    NOT_APPROVED a destination exists but is not approved to receive this
                 entity's financial data
    READY        submission has a defined, approved destination
    """

    NOT_BOUND = 'NOT_BOUND'
    NOT_APPROVED = 'NOT_APPROVED'
    READY = 'READY'


@strawberry.type
class PlanningAnalyticsTarget:
    context: ResolvedFinancialContext
    state: PlanningTargetState
    can_submit: bool
    # Present whenever the destination is not ready, in the server's words.
    user_safe_reason: Optional[str]
    # Names only. The TM1 server's base URL and credentials are never exposed:
    # TM1 is reachable from inside the network alone, and the browser needs to
    # know where a submission goes, not how to reach it.
    display_name: Optional[str]
    workspace: Optional[str]
    default_scenario: Optional[str]
    default_version: Optional[str]
    approved_at: Optional[datetime.datetime]
    approval_note: Optional[str]
