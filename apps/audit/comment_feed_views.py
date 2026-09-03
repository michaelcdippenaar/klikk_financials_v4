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

All three comment tables — receipts, findings, and replies on the cube-comment
register — are read, merged, sorted by created_at and capped at ``FEED_MAX``. A client that has been away long enough to overflow the cap
gets the OLDEST events in the window and a cursor that advances only to the
last event returned, so nothing is skipped — it just catches up over several
polls.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.receipts.models import SlipComment
from apps.xero.xero_data import cube_comment_replies

from .comment_events import (
    CUBE_COMMENT, FINDING, RECEIPT, comment_payload, finding_ref, receipt_ref,
)
from .models import AuditFindingComment

logger = logging.getLogger(__name__)

FEED_MAX = 200
# How far back an omitted / unusable `since` looks. Small on purpose: a first
# poll should prime the cursor, not replay the week.
DEFAULT_WINDOW = dt.timedelta(minutes=5)


def _event_id(comment):
    """Tie-break for the merge sort. A cube reply is a dict, not a model."""
    if isinstance(comment, dict):
        return comment['reply']['id']
    return comment.id


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

    # Cube-comment replies come off raw SQL rather than a model, already carrying
    # the parent comment's id and label — the feed must not have to re-resolve
    # either. A dead register must not take the whole feed down with it: the other
    # two surfaces are still worth delivering.
    # The savepoint is load-bearing, not defensive habit: in PostgreSQL a failed
    # statement aborts the whole transaction, so catching the Python exception
    # alone would leave every LATER query in this request answering
    # InFailedSqlTransaction. Rolling back to a savepoint keeps the failure
    # local to this read, which is what makes the fallback below true.
    try:
        with transaction.atomic():
            cube_replies = cube_comment_replies.replies_since(since, FEED_MAX + 1)
    except Exception:  # noqa: BLE001
        logger.exception('comment feed: could not read cube-comment replies')
        cube_replies = []

    merged = [(c.created_at, RECEIPT, c) for c in slip_comments]
    merged += [(c.created_at, FINDING, c) for c in finding_comments]
    merged += [(row['created_at'], CUBE_COMMENT, row) for row in cube_replies]
    merged.sort(key=lambda row: (row[0], row[1], _event_id(row[2])))

    truncated = len(merged) > FEED_MAX
    merged = merged[:FEED_MAX]

    events = []
    # One ref lookup per distinct object, not per comment — a burst of replies
    # on one receipt would otherwise hit klikk_slips once each.
    refs: dict[tuple[str, str], str] = {}
    for created_at, kind, comment in merged:
        if kind == CUBE_COMMENT:
            # The OBJECT is the comment the reply hangs off, not the reply — a
            # console following object_id must land on the thread.
            object_id = str(comment['comment_id'])
            key = (kind, object_id)
            refs.setdefault(key, comment['ref'])
            comment = comment['reply']
        elif kind == RECEIPT:
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
