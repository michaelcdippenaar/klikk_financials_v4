"""
``record_activity`` — the one way anything writes to the activity trail.

The contract that matters: **it never raises.** Every call site is inside a
user-facing write, and a trail that can fail a receipt archive or lose a
comment is worse than no trail at all. Every failure is logged and swallowed.

The actor is resolved with the SAME function the auditor gate uses
(apps.user.middleware.resolve_request_user), because two different answers to
"who is this request" is how gates and audit trails end up disagreeing.
"""
from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

MAX_USER_AGENT = 300
MAX_TARGET_REF = 300
# Bulk events carry ids for reconstruction, not for storage — a 500-row action
# is one event, and the id list is capped so one action cannot bloat the row.
MAX_BULK_IDS = 500


def _client_ip(request):
    """Left-most X-Forwarded-For entry when behind nginx, else REMOTE_ADDR."""
    forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    candidate = forwarded or (request.META.get('REMOTE_ADDR') or '').strip()
    return candidate or None


def _is_service_account(user):
    """Service-token callers (the MCP server, agents) authenticate as the shared
    ServiceAccount stand-in — the same rule the comment views use for `author`."""
    try:
        from klikk_business_intelligence.permissions import ServiceAccount
        return isinstance(user, ServiceAccount)
    except Exception:  # noqa: BLE001 — import shape is not worth failing over
        return False


def _source(request, user, override=None):
    if override:
        return override
    return 'mcp' if _is_service_account(user) else 'console'


def _actor_name(user):
    """Username for the trail.

    ServiceAccount is NOT a Django user — it has no ``get_username`` — so it is
    recorded as 'mcp', matching how the comment views stamp `author`. Without
    this every MCP write raised inside the recorder and was silently swallowed,
    which is precisely the class of gap this trail exists to close.
    """
    if user is None:
        return ''
    if _is_service_account(user):
        return 'mcp'
    getter = getattr(user, 'get_username', None)
    if callable(getter):
        return getter() or ''
    return str(getattr(user, 'username', '') or '')


def jsonable(value):
    """Decimals / dates / models -> something JSONField can store."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def diff(before: dict, after: dict, fields) -> dict:
    """{field: {from, to}} for the fields that ACTUALLY changed.

    Unchanged fields are omitted: an event saying "status: OPEN -> OPEN" is
    noise that makes the real changes harder to see.
    """
    out = {}
    for field in fields:
        old, new = before.get(field), after.get(field)
        if old != new:
            out[field] = {'from': jsonable(old), 'to': jsonable(new)}
    return out


def record_activity(request, action, *, target_kind='', target_id='', target_ref='',
                    changes=None, source=None, actor=None):
    """Append one event. Returns the row, or None if anything went wrong.

    ``request`` may be None for system-originated events.
    """
    try:
        from apps.user.middleware import resolve_request_user

        from .models import ActivityEvent

        user = resolve_request_user(request) if request is not None else None
        username = actor if actor is not None else _actor_name(user)
        # Only a real auth-user row can go in the FK; the MCP ServiceAccount
        # stand-in is not a User and would blow up the insert.
        actor_user = user if (user is not None and getattr(user, 'pk', None)) else None
        try:
            from django.contrib.auth import get_user_model
            if not isinstance(actor_user, get_user_model()):
                actor_user = None
        except Exception:  # noqa: BLE001
            actor_user = None

        meta = request.META if request is not None else {}
        return ActivityEvent.objects.create(
            actor=(username or '')[:150],
            actor_user=actor_user,
            actor_role=(getattr(user, 'role', '') or '')[:16],
            action=action[:48],
            target_kind=(target_kind or '')[:16],
            target_id=str(target_id or '')[:64],
            target_ref=(target_ref or '')[:MAX_TARGET_REF],
            changes=changes,
            source=_source(request, user, source),
            ip=_client_ip(request) if request is not None else None,
            user_agent=(meta.get('HTTP_USER_AGENT') or '')[:MAX_USER_AGENT],
            request_id=(meta.get('HTTP_X_REQUEST_ID') or '')[:64],
        )
    except Exception:  # noqa: BLE001 — see the module docstring
        logger.exception('Could not record activity %s on %s %s', action, target_kind, target_id)
        return None


def record_auditor_read(request, action, **kwargs):
    """Record a READ, but only for auditor accounts.

    Standard users' reads are deliberately not logged: the point is
    accountability for the external parties given access to the books, not
    surveillance of the team. Signed-URL file views may carry no JWT at all —
    those resolve to no user and are skipped rather than logged as anonymous.
    """
    try:
        from apps.user.middleware import resolve_request_user

        user = resolve_request_user(request) if request is not None else None
        if user is None or getattr(user, 'role', None) != 'auditor':
            return None
        return record_activity(request, action, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception('Could not record auditor read %s', action)
        return None


def bulk_changes(ids, **extra):
    """``changes`` for a bulk action: the ids, the count, and the values applied."""
    ids = list(ids or [])
    payload = {'count': len(ids), 'ids': [jsonable(i) for i in ids[:MAX_BULK_IDS]]}
    if len(ids) > MAX_BULK_IDS:
        payload['ids_truncated'] = True
    payload.update({k: jsonable(v) for k, v in extra.items() if v is not None})
    return payload
