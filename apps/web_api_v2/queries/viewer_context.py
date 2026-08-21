import strawberry
from django.db.models import Prefetch

from apps.web_api_v2.models import (
    UserEntityCapability,
    UserEntityMembership,
    ViewerPreference,
)
from apps.web_api_v2.services.entity_access import capability_codes_for_membership
from apps.web_api_v2.types.viewer import (
    EntityCapability,
    EntityOption,
    EntityRole,
    EntitySelectionState,
    EntityStatus,
    Viewer,
    ViewerContext,
    ViewerPreferences,
)


def build_viewer_context(info) -> ViewerContext:
    user = info.context.request.user
    memberships = list(
        UserEntityMembership.objects.filter(user=user, active=True)
        .select_related('entity')
        .prefetch_related(Prefetch(
            'capability_grants',
            queryset=UserEntityCapability.objects.filter(active=True),
        ))
        .order_by('entity__tenant_name', 'entity_id')
    )
    allowed_entity_ids = {membership.entity_id for membership in memberships}
    preference = (
        ViewerPreference.objects.filter(user=user)
        .only('default_entity_id', 'default_financial_year')
        .first()
    )

    default_entity_id = None
    default_financial_year = None
    if preference is not None:
        if preference.default_entity_id in allowed_entity_ids:
            default_entity_id = strawberry.ID(preference.default_entity_id)
        default_financial_year = preference.default_financial_year

    display_name = user.get_full_name().strip() or user.username
    return ViewerContext(
        user=Viewer(
            id=strawberry.ID(str(user.pk)),
            username=user.username,
            display_name=display_name,
            email=user.email or None,
        ),
        entities=[
            EntityOption(
                id=strawberry.ID(membership.entity_id),
                name=membership.entity.tenant_name,
                role=EntityRole(membership.role),
                active=membership.active,
                status=(
                    EntityStatus.REAUTHORIZATION_REQUIRED
                    if membership.entity.reauth_required
                    else EntityStatus.AVAILABLE
                ),
                capabilities=[
                    EntityCapability(code)
                    for code in capability_codes_for_membership(membership)
                ],
            )
            for membership in memberships
        ],
        entity_selection_state=(
            EntitySelectionState.READY
            if memberships
            else EntitySelectionState.NO_ACCESSIBLE_ENTITIES
        ),
        preferences=ViewerPreferences(
            default_entity_id=default_entity_id,
            default_financial_year=default_financial_year,
        ),
    )
