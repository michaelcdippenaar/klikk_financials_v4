from enum import Enum
from typing import Optional

import strawberry


@strawberry.enum
class EntityRole(Enum):
    OWNER = 'OWNER'
    ADMIN = 'ADMIN'
    REVIEWER = 'REVIEWER'
    VIEWER = 'VIEWER'


@strawberry.enum
class EntitySelectionState(Enum):
    READY = 'READY'
    NO_ACCESSIBLE_ENTITIES = 'NO_ACCESSIBLE_ENTITIES'


@strawberry.enum
class EntityStatus(Enum):
    AVAILABLE = 'AVAILABLE'
    REAUTHORIZATION_REQUIRED = 'REAUTHORIZATION_REQUIRED'


@strawberry.enum
class EntityCapability(Enum):
    # Expand only when matching backend permission checks are implemented.
    VIEW_FINANCIALS = 'VIEW_FINANCIALS'


@strawberry.type
class Viewer:
    id: strawberry.ID
    username: str
    display_name: str
    email: Optional[str]


@strawberry.type
class EntityOption:
    id: strawberry.ID
    name: str
    role: EntityRole
    active: bool
    status: EntityStatus
    capabilities: list[EntityCapability]


@strawberry.type
class ViewerPreferences:
    default_entity_id: Optional[strawberry.ID]
    default_financial_year: Optional[int]


@strawberry.type
class ViewerContext:
    user: Viewer
    entities: list[EntityOption]
    entity_selection_state: EntitySelectionState
    preferences: ViewerPreferences
