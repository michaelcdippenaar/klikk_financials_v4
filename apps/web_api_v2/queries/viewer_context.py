import logging

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

logger = logging.getLogger(__name__)


def _capabilities(membership):
    """The membership's granted capabilities, minus any this schema cannot name.

    A capability can exist in the database before the schema knows the word for
    it — a grant applied ahead of the deploy that adds it, or one added by hand.
    Passing an unknown code straight to the enum raises ValueError, and because
    this builds the whole viewer context, that one unknown grant took down the
    entity list entirely: a signed-in user with three live memberships saw no
    entities at all and no reason why.

    A capability the schema cannot express is one the client could not act on
    anyway, so dropping it costs nothing and keeps every other entity visible.
    It is logged, because a permission silently not taking effect is worth
    knowing about.
    """
    known = []
    for code in capability_codes_for_membership(membership):
        try:
            known.append(EntityCapability(code))
        except ValueError:
            logger.warning(
                'viewer_context_unknown_capability entity=%s code=%s',
                membership.entity_id,
                code,
            )
    return known


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
                capabilities=_capabilities(membership),
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
