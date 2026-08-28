"""The entity's Planning Analytics destination, in presentation-safe form.

TM1 is reachable only from inside the network and its credentials live on
TM1ServerConfig. Nothing here returns a base URL, username or password: the
browser gets the destination's names and its approval state, which is all it
needs to say where a submission would go and whether it may go there.

Three states, kept distinct because collapsing them hides the actual problem:

    no binding      nobody has said where this entity's data should go
    not approved    a destination exists but has not been approved to receive
                    this entity's financial data
    approved        submission has a defined, approved destination
"""
from apps.planning_analytics.models import EntityPlanningTarget

STATE_NOT_BOUND = 'NOT_BOUND'
STATE_NOT_APPROVED = 'NOT_APPROVED'
STATE_READY = 'READY'

REASONS = {
    STATE_NOT_BOUND: (
        'No Planning Analytics destination is bound to this entity, so a '
        'submission has nowhere defined to go.'
    ),
    STATE_NOT_APPROVED: (
        'This entity\'s Planning Analytics destination has not been approved to '
        'receive its financial data.'
    ),
}

# Fields that must never reach the browser. Asserted by a test rather than
# left to reviewer memory.
SERVER_SECRET_FIELDS = ('base_url', 'username', 'password')


def describe_target(entity):
    """Presentation-safe description of where this entity would submit."""
    target = EntityPlanningTarget.for_entity(entity.pk)
    if target is None:
        return {
            'state': STATE_NOT_BOUND,
            'userSafeReason': REASONS[STATE_NOT_BOUND],
            'displayName': None,
            'workspace': None,
            'defaultScenario': None,
            'defaultVersion': None,
            'approvedAt': None,
            'approvalNote': None,
        }

    state = STATE_READY if target.approved else STATE_NOT_APPROVED
    return {
        'state': state,
        'userSafeReason': REASONS.get(state),
        # Names only. The server row's connection details stay server-side.
        'displayName': target.display_name,
        'workspace': target.workspace,
        'defaultScenario': target.default_scenario or None,
        'defaultVersion': target.default_version or None,
        'approvedAt': target.approved_at,
        'approvalNote': target.approval_note or None,
    }


def entity_can_submit(entity):
    """True only for an approved destination. Bound is not approved."""
    return describe_target(entity)['state'] == STATE_READY
