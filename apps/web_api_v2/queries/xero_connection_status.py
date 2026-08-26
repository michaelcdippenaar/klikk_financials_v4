from graphql import GraphQLError

from apps.web_api_v2.services.entity_access import (
    VIEW_FINANCIALS_CAPABILITY,
    capability_codes_for_membership,
)
from apps.web_api_v2.services.fiscal_context import resolve_financial_context
from apps.web_api_v2.services.xero_connection_status import (
    build_xero_connection_status as build_status,
)


def _require_view_financials(info, membership):
    if VIEW_FINANCIALS_CAPABILITY in capability_codes_for_membership(membership):
        return
    reason = 'You do not have permission to view Xero connection status.'
    raise GraphQLError(
        reason,
        extensions={
            'code': 'PERMISSION_DENIED',
            'correlationId': getattr(
                info.context.request,
                'graphql_correlation_id',
                '-',
            ),
            'retryable': False,
            'userSafeReason': reason,
        },
    )


def build_xero_connection_status(info, context_input):
    membership, resolved_context = resolve_financial_context(info, context_input)
    _require_view_financials(info, membership)
    return build_status(membership.entity, resolved_context)
