"""
GET /audit/comments/feed/?since=<ISO-8601>

The live-comment feed the console polls so a comment left by someone else
surfaces without a reload. Deliberately polling, not websockets: adding
Channels + Redis to carry a handful of comment events would be a new
always-on dependency (and a new failure mode) for something a 5-second poll
answers completely.

Two things make the cursor safe:

  * the response carries ``now`` — the SERVER's clock — and the client sends
    that back as the next ``since``. A client whose clock is skewed would
    otherwise re-fetch the same events forever, or skip some.
  * ``since`` is EXCLUSIVE (``created_at__gt``), so an event is never
    delivered twice on consecutive polls.

Mounted under /audit/ on purpose: auditors comment now, so they need to see
replies. The auditor gate already allows GETs under this prefix.

Both comment tables are read, merged, sorted by created_at and capped at
``FEED_MAX``. A client that has been away long enough to overflow the cap
gets the OLDEST events in the window and a cursor that advances only to the
last event returned, so nothing is skipped — it just catches up over several
polls.
"""
from __future__ import annotations

import datetime as dt

from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.receipts.models import SlipComment

from .comment_events import FINDING, RECEIPT, comment_payload, finding_ref, receipt_ref
from .models import AuditFindingComment

FEED_MAX = 200
# How far back an omitted / unusable `since` looks. Small on purpose: a first
# poll should prime the cursor, not replay the week.
DEFAULT_WINDOW = dt.timedelta(minutes=5)


def _parse_since(raw):
    """(since, error) — an unparseable cursor is a 400, never a silent full scan."""
    if raw in (None, ''):
        return None, None
    parsed = parse_datetime(str(raw))
    if parsed is None:
        return None, 'since must be an ISO-8601 timestamp'
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed, None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def comment_feed_view(request):
    since, error = _parse_since(request.query_params.get('since'))
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)

    now = dt.datetime.now(dt.timezone.utc)
    if since is None:
        since = now - DEFAULT_WINDOW

    # +1 on each side so the merged list can genuinely overflow FEED_MAX and be
    # detected, rather than silently arriving exactly full.
    slip_comments = list(
        SlipComment.objects.filter(created_at__gt=since).order_by('created_at')[: FEED_MAX + 1]
    )
    finding_comments = list(
        AuditFindingComment.objects.filter(created_at__gt=since)
        .select_related('finding')
        .order_by('created_at')[: FEED_MAX + 1]
    )

    merged = [(c.created_at, RECEIPT, c) for c in slip_comments]
    merged += [(c.created_at, FINDING, c) for c in finding_comments]
    merged.sort(key=lambda row: (row[0], row[1], row[2].id))

    truncated = len(merged) > FEED_MAX
    merged = merged[:FEED_MAX]

    events = []
    # One ref lookup per distinct object, not per comment — a burst of replies
    # on one receipt would otherwise hit klikk_slips once each.
    refs: dict[tuple[str, str], str] = {}
    for created_at, kind, comment in merged:
        if kind == RECEIPT:
            object_id = comment.sha256
            key = (kind, object_id)
            if key not in refs:
                refs[key] = receipt_ref(object_id)
        else:
            object_id = str(comment.finding_id)
            key = (kind, object_id)
            if key not in refs:
                refs[key] = finding_ref(comment.finding)
        events.append({
            'kind': kind,
            'object_id': object_id,
            'object_ref': refs[key],
            'comment': comment_payload(comment),
        })

    # When the window overflowed, the cursor advances only as far as the last
    # event actually delivered — otherwise the untold events would be skipped.
    cursor = merged[-1][0].isoformat() if truncated and merged else now.isoformat()

    return Response({
        'now': cursor,
        'server_time': now.isoformat(),
        'truncated': truncated,
        'events': events,
    })
