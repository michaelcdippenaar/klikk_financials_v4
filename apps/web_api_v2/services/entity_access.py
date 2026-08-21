from graphql import GraphQLError

from apps.web_api_v2.models import UserEntityCapability, UserEntityMembership


VIEW_FINANCIALS_CAPABILITY = 'VIEW_FINANCIALS'
RUN_INGESTION_PROCESS_CAPABILITY = 'RUN_INGESTION_PROCESS'


class EntityAccessDenied(Exception):
    pass


class EntityCapabilityDenied(Exception):
    pass


def require_entity_membership(user, entity_id):
    membership = (
        UserEntityMembership.objects.select_related('entity')
        .prefetch_related('capability_grants')
        .filter(user=user, entity_id=str(entity_id), active=True)
        .first()
    )
    if membership is None:
        raise EntityAccessDenied
    return membership


def capability_codes_for_membership(membership):
    if not membership.active:
        return ()
    grants = getattr(membership, '_prefetched_objects_cache', {}).get('capability_grants')
    if grants is None:
        grants = membership.capability_grants.all()
    explicit = {grant.code for grant in grants if grant.active}
    return (VIEW_FINANCIALS_CAPABILITY, *sorted(explicit))


def require_entity_capability(user, entity_id, capability_code):
    membership = require_entity_membership(user, entity_id)
    if capability_code not in capability_codes_for_membership(membership):
        raise EntityCapabilityDenied
    return membership


def require_entity_access(info, entity_id):
    """GraphQL wrapper over the shared transport-neutral membership check."""
    try:
        return require_entity_membership(info.context.request.user, entity_id)
    except EntityAccessDenied:
        correlation_id = getattr(info.context.request, 'graphql_correlation_id', '-')
        raise GraphQLError(
            'You do not have access to this entity.',
            extensions={
                'code': 'FORBIDDEN_ENTITY',
                'correlationId': correlation_id,
            },
        ) from None
