"""
Read endpoints for the activity trail.

Mounted at ``/api/activity/`` — deliberately OUTSIDE ``/audit/``. Auditors are
the accounts this trail exists to record, and the auditor gate already 403s
every non-/audit/ path, so the mount point IS the access control: an auditor
cannot read their own surveillance trail, or anyone else's.

Read-only. Nothing in this app writes through an endpoint; the only writer is
``record_activity``, called from the write paths themselves.
"""
from __future__ import annotations

import csv
import datetime as dt
import json

from django.db.models import Q
from django.http import StreamingHttpResponse
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ACTIONS, ActivityEvent

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
EXPORT_MAX = 10_000

CSV_COLUMNS = (
    'occurred_at', 'actor', 'actor_role', 'action', 'target_kind', 'target_id',
    'target_ref', 'changes', 'source', 'ip', 'user_agent', 'request_id',
)


def _int(raw, default, *, low, high):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _moment(raw, *, end_of_day=False):
    """Accept a date OR a datetime; a bare date on `until` means end of day.

    Otherwise `until=2026-08-20` would silently exclude everything that
    happened on the 20th, which is never what the person filtering meant.
    """
    if not raw:
        return None
    parsed = parse_datetime(str(raw))
    if parsed is None:
        day = parse_date(str(raw))
        if day is None:
            return None
        parsed = dt.datetime.combine(
            day, dt.time.max if end_of_day else dt.time.min)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _filtered(params):
    """(queryset, error). Unparseable dates are a 400, never a silent full scan."""
    qs = ActivityEvent.objects.all()

    actor = (params.get('actor') or '').strip()
    if actor:
        qs = qs.filter(actor=actor)

    # Repeatable ?action= (the console sends a multi-select).
    actions = [a for a in params.getlist('action') if a] if hasattr(params, 'getlist') else []
    if actions:
        qs = qs.filter(action__in=actions)

    target_kind = (params.get('target_kind') or '').strip()
    if target_kind:
        qs = qs.filter(target_kind=target_kind)

    target_id = (params.get('target_id') or '').strip()
    if target_id:
        qs = qs.filter(target_id=target_id)

    for key, kwarg, end in (('since', 'occurred_at__gte', False),
                            ('until', 'occurred_at__lte', True)):
        raw = params.get(key)
        if raw:
            moment = _moment(raw, end_of_day=end)
            if moment is None:
                return None, f'{key} must be a date or ISO-8601 timestamp'
            qs = qs.filter(**{kwarg: moment})

    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(actor__icontains=q) | Q(action__icontains=q)
            | Q(target_ref__icontains=q) | Q(target_id__icontains=q)
        )
    return qs, None


def event_to_dict(e: ActivityEvent) -> dict:
    return {
        'id': e.id,
        'occurred_at': e.occurred_at.isoformat() if e.occurred_at else None,
        'actor': e.actor,
        'actor_role': e.actor_role,
        'action': e.action,
        'target_kind': e.target_kind,
        'target_id': e.target_id,
        'target_ref': e.target_ref,
        'changes': e.changes,
        'source': e.source,
        'ip': e.ip,
        'user_agent': e.user_agent,
        'request_id': e.request_id,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_list_view(request):
    qs, error = _filtered(request.query_params)
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)

    page_size = _int(request.query_params.get('page_size'), DEFAULT_PAGE_SIZE,
                     low=1, high=MAX_PAGE_SIZE)
    page = _int(request.query_params.get('page'), 1, low=1, high=1_000_000)

    count = qs.count()
    start = (page - 1) * page_size
    results = [event_to_dict(e) for e in qs[start:start + page_size]]
    num_pages = max(1, -(-count // page_size))  # ceil
    return Response({
        'count': count,
        'page': page,
        'page_size': page_size,
        'num_pages': num_pages,
        'results': results,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_actors_view(request):
    """Distinct actors, for the filter dropdown."""
    actors = (ActivityEvent.objects.exclude(actor='')
              .values_list('actor', flat=True).distinct().order_by('actor'))
    return Response({'actors': list(actors)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_actions_view(request):
    """The known action slugs, so the console's filter cannot drift from the app."""
    return Response({'actions': list(ACTIONS)})


class _Echo:
    """csv.writer sink for StreamingHttpResponse."""

    def write(self, value):
        return value


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_export_view(request):
    """CSV of the same filter, unpaged (capped at EXPORT_MAX rows).

    Streamed rather than assembled: an unpaged trail export is exactly the
    thing that quietly turns into a 200MB string in memory.
    """
    qs, error = _filtered(request.query_params)
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)

    writer = csv.writer(_Echo())

    def rows():
        yield writer.writerow(CSV_COLUMNS)
        for e in qs[:EXPORT_MAX].iterator(chunk_size=500):
            data = event_to_dict(e)
            yield writer.writerow([
                data['occurred_at'], data['actor'], data['actor_role'], data['action'],
                data['target_kind'], data['target_id'], data['target_ref'],
                '' if data['changes'] is None else json.dumps(data['changes']),
                data['source'], data['ip'], data['user_agent'], data['request_id'],
            ])

    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M')
    response = StreamingHttpResponse(rows(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="activity-{stamp}.csv"'
    return response
