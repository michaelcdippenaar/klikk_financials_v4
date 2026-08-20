from graphql import GraphQLError

from apps.web_api_v2.models import UserEntityMembership


VIEW_FINANCIALS_CAPABILITY = 'VIEW_FINANCIALS'


def capability_codes_for_membership(membership):
    """Return only capabilities backed by current server-side checks."""
    return (VIEW_FINANCIALS_CAPABILITY,) if membership.active else ()


def require_entity_access(info, entity_id):
    """Return an active membership or fail before finance data is queried."""
    user = info.context.request.user
    membership = (
        UserEntityMembership.objects.select_related('entity')
        .filter(user=user, entity_id=str(entity_id), active=True)
        .first()
    )
    if membership is None:
        correlation_id = getattr(info.context.request, 'graphql_correlation_id', '-')
        raise GraphQLError(
            'You do not have access to this entity.',
            extensions={
                'code': 'FORBIDDEN_ENTITY',
                'correlationId': correlation_id,
            },
        )
    return membership
