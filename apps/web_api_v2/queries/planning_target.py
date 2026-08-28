from apps.web_api_v2.queries.xero_pipeline import _graphql_error
from apps.web_api_v2.services.entity_access import (
    VIEW_FINANCIALS_CAPABILITY,
    capability_codes_for_membership,
)
from apps.web_api_v2.services.fiscal_context import resolve_financial_context
from apps.web_api_v2.services.planning_target import describe_target
from apps.web_api_v2.types.planning_target import (
    PlanningAnalyticsTarget,
    PlanningTargetState,
)


def build_planning_analytics_target(info, context_input):
    membership, context = resolve_financial_context(info, context_input)
    if VIEW_FINANCIALS_CAPABILITY not in capability_codes_for_membership(membership):
        _graphql_error(info, 'CAPABILITY_REQUIRED', 'VIEW_FINANCIALS capability is required.')

    described = describe_target(membership.entity)
    state = PlanningTargetState(described['state'])
    return PlanningAnalyticsTarget(
        context=context,
        state=state,
        can_submit=state is PlanningTargetState.READY,
        user_safe_reason=described['userSafeReason'],
        display_name=described['displayName'],
        workspace=described['workspace'],
        default_scenario=described['defaultScenario'],
        default_version=described['defaultVersion'],
        approved_at=described['approvedAt'],
        approval_note=described['approvalNote'],
    )
