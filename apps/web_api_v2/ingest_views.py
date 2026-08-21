import base64
import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.exceptions import APIException, AuthenticationFailed, NotAuthenticated, ParseError, Throttled
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.web_api_v2.api_errors import error_response, request_correlation_id
from apps.web_api_v2.models import (
    IngestProcessAuditEvent,
    IngestProcessRun,
    IngestProcessRunPeriod,
)
from apps.web_api_v2.services.entity_access import (
    EntityAccessDenied,
    EntityCapabilityDenied,
    RUN_INGESTION_PROCESS_CAPABILITY,
    require_entity_capability,
)
from apps.web_api_v2.services.ingest_registry import (
    PROCESS_DEFINITIONS,
    ProcessCommandError,
    execute_process,
    first_blocking_prerequisite,
    prerequisite_status,
)


logger = logging.getLogger(__name__)
RETRY_SOURCE_STATES = {
    IngestProcessRun.State.FAILED,
    IngestProcessRun.State.CANCELLED,
    IngestProcessRun.State.BLOCKED,
}


class RequestTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = 'Request body is too large.'


def _iso(value):
    return value.isoformat() if value else None


def _display_name(user):
    return user.get_full_name().strip() or user.username


def _latest_success(run):
    return IngestProcessRun.objects.filter(
        entity=run.entity,
        process_key=run.process_key,
        state=IngestProcessRun.State.SUCCEEDED,
    ).order_by('-finished_at').values_list('finished_at', flat=True).first()


def serialize_run(run, *, include_idempotency=False, idempotent_replay=False):
    payload = {
        'id': str(run.pk),
        'entityId': str(run.entity_id),
        'processKey': run.process_key,
        'displayName': PROCESS_DEFINITIONS[run.process_key]['display_name'],
        'state': run.state,
        'requestedAt': _iso(run.requested_at),
        'queuedAt': _iso(run.queued_at),
        'startedAt': _iso(run.started_at),
        'finishedAt': _iso(run.finished_at),
        'leaseExpiresAt': _iso(run.lease_expires_at),
        'latestSuccessAt': _iso(_latest_success(run)),
        'actor': {'id': str(run.actor_id), 'displayName': _display_name(run.actor)},
        'periods': list(run.periods or []),
        'records': run.records_summary or {},
        'outputSummary': run.output_summary or {},
        'prerequisites': prerequisite_status(run.entity, run.process_key),
        'permittedActions': _permitted_actions(run),
        'retryable': run.retryable,
        'blockedReason': run.blocked_reason,
        'error': ({
            'code': run.error_code,
            'message': run.error_message,
            'correlationId': str(run.correlation_id),
        } if run.error_code else None),
        'retryOfRunId': str(run.retry_of_id) if run.retry_of_id else None,
        'idempotentReplay': idempotent_replay,
    }
    if include_idempotency:
        payload['idempotencyKey'] = run.idempotency_key
    return payload


def _permitted_actions(run):
    if run.state in (IngestProcessRun.State.QUEUED, IngestProcessRun.State.RUNNING):
        return []
    if first_blocking_prerequisite(run.entity, run.process_key):
        return []
    actions = ['run']
    if run.state in (IngestProcessRun.State.FAILED, IngestProcessRun.State.BLOCKED) and run.retryable:
        actions.append('retry')
    return actions


def _audit(run, action):
    IngestProcessAuditEvent.objects.create(
        run=run,
        entity=run.entity,
        actor=run.actor,
        process_key=run.process_key,
        action=action,
        result_state=run.state,
        correlation_id=run.correlation_id,
        safe_metadata={'retryOfRunId': str(run.retry_of_id) if run.retry_of_id else None},
    )


def _replace_run_periods(run, periods):
    normalized = sorted(set(periods))
    run.run_periods.exclude(period__in=normalized).delete()
    IngestProcessRunPeriod.objects.bulk_create(
        [IngestProcessRunPeriod(run=run, period=period) for period in normalized],
        ignore_conflicts=True,
    )


def _expire_stale_active_runs(entity):
    stale_runs = list(IngestProcessRun.objects.select_for_update().filter(
        entity=entity,
        state__in=(IngestProcessRun.State.QUEUED, IngestProcessRun.State.RUNNING),
        lease_expires_at__lt=timezone.now(),
    ).select_related('actor', 'entity'))
    for stale in stale_runs:
        stale.state = IngestProcessRun.State.FAILED
        stale.finished_at = timezone.now()
        stale.error_code = 'RUN_LEASE_EXPIRED'
        stale.error_message = 'The process run lease expired before completion.'
        stale.retryable = True
        stale.save(update_fields=(
            'state', 'finished_at', 'error_code', 'error_message', 'retryable',
        ))
        _audit(stale, 'lease-expired')


def _fingerprint(payload):
    normalized = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _encode_cursor(run):
    raw = json.dumps({'at': run.requested_at.isoformat(), 'id': str(run.pk)}, separators=(',', ':'))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')


def _decode_cursor(value):
    try:
        padded = value + '=' * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        timestamp = parse_datetime(payload['at'])
        run_id = uuid.UUID(payload['id'])
        if timestamp is None:
            raise ValueError
        return timestamp, run_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError('Invalid cursor.') from None


class V2IngestView(APIView):
    authentication_classes = (JWTAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'v2_ingest_reads'

    def initial(self, request, *args, **kwargs):
        content_length = request.META.get('CONTENT_LENGTH')
        try:
            if content_length and int(content_length) > settings.WEB_API_V2_INGEST_MAX_REQUEST_BYTES:
                raise RequestTooLarge
        except ValueError:
            raise ParseError('Invalid Content-Length header.') from None
        return super().initial(request, *args, **kwargs)

    def get_throttles(self):
        self.throttle_scope = (
            'v2_ingest_commands'
            if self.request.method == 'POST'
            else 'v2_ingest_reads'
        )
        return super().get_throttles()

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['X-Correlation-ID'] = request_correlation_id(request)
        return response

    def handle_exception(self, exc):
        if isinstance(exc, (AuthenticationFailed, NotAuthenticated)):
            return error_response(
                self.request, 'UNAUTHENTICATED',
                'Authentication credentials were not accepted.',
                status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, Throttled):
            return error_response(
                self.request, 'RATE_LIMITED', 'Too many requests.',
                status.HTTP_429_TOO_MANY_REQUESTS, retryable=True,
            )
        if isinstance(exc, ParseError):
            return error_response(
                self.request, 'VALIDATION_ERROR', 'Request body is not valid JSON.',
                status.HTTP_400_BAD_REQUEST,
            )
        if isinstance(exc, RequestTooLarge):
            return error_response(
                self.request, 'VALIDATION_ERROR', 'Ingest request is too large.',
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if isinstance(exc, DatabaseError):
            logger.error(
                'v2_ingest_unavailable user=%s correlation_id=%s',
                getattr(getattr(self.request, 'user', None), 'pk', '-'),
                request_correlation_id(self.request),
            )
            return error_response(
                self.request, 'TEMPORARILY_UNAVAILABLE',
                'Ingest data is temporarily unavailable.',
                status.HTTP_503_SERVICE_UNAVAILABLE, retryable=True,
            )
        return super().handle_exception(exc)

    def _authorize(self, request, entity_id):
        try:
            return require_entity_capability(
                request.user,
                entity_id,
                RUN_INGESTION_PROCESS_CAPABILITY,
            )
        except EntityAccessDenied:
            return error_response(
                request, 'FORBIDDEN_ENTITY', 'You do not have access to this entity.',
                status.HTTP_403_FORBIDDEN,
            )
        except EntityCapabilityDenied:
            return error_response(
                request, 'CAPABILITY_REQUIRED',
                'RUN_INGESTION_PROCESS capability is required.',
                status.HTTP_403_FORBIDDEN,
            )


class ProcessRunListCreateView(V2IngestView):
    def get(self, request, entity_id):
        membership = self._authorize(request, entity_id)
        if isinstance(membership, Response):
            return membership
        queryset = IngestProcessRun.objects.filter(entity=membership.entity).select_related('actor', 'entity')
        process_key = request.query_params.get('processKey')
        state_filter = request.query_params.get('state')
        if process_key:
            if process_key not in PROCESS_DEFINITIONS:
                return error_response(
                    request, 'UNKNOWN_PROCESS', 'The requested process is not allowlisted.',
                    status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(process_key=process_key)
        if state_filter:
            if state_filter not in IngestProcessRun.State.values:
                return error_response(
                    request, 'VALIDATION_ERROR', 'Unknown run state.',
                    status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(state=state_filter)
        try:
            limit = int(request.query_params.get('limit', 20))
        except ValueError:
            limit = 0
        if not 1 <= limit <= 50:
            return error_response(
                request, 'VALIDATION_ERROR', 'limit must be between 1 and 50.',
                status.HTTP_400_BAD_REQUEST,
            )
        cursor = request.query_params.get('cursor')
        if cursor:
            try:
                cursor_at, cursor_id = _decode_cursor(cursor)
            except ValueError:
                return error_response(
                    request, 'VALIDATION_ERROR', 'cursor is invalid.',
                    status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(
                Q(requested_at__lt=cursor_at)
                | Q(requested_at=cursor_at, id__lt=cursor_id)
            )
        rows = list(queryset[:limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        return Response({
            'results': [serialize_run(run) for run in rows],
            'nextCursor': _encode_cursor(rows[-1]) if has_more and rows else None,
        })

    def post(self, request, entity_id):
        membership = self._authorize(request, entity_id)
        if isinstance(membership, Response):
            return membership
        if not isinstance(request.data, dict):
            return error_response(
                request, 'VALIDATION_ERROR', 'Request body must be an object.',
                status.HTTP_400_BAD_REQUEST,
            )
        process_key = request.data.get('processKey')
        idempotency_key = request.data.get('idempotencyKey')
        periods = request.data.get('periods', [])
        expected_state = request.data.get('expectedState') or {}
        retry_of_raw = request.data.get('retryOfRunId')

        if process_key not in PROCESS_DEFINITIONS:
            return error_response(
                request, 'UNKNOWN_PROCESS', 'The requested process is not allowlisted.',
                status.HTTP_400_BAD_REQUEST,
            )
        if (
            not isinstance(idempotency_key, str)
            or not 8 <= len(idempotency_key) <= 128
            or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-'
                   for character in idempotency_key)
        ):
            return error_response(
                request, 'VALIDATION_ERROR',
                'idempotencyKey must be 8-128 safe opaque characters.',
                status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(periods, list) or len(periods) > 12:
            return error_response(
                request, 'VALIDATION_ERROR', 'periods must contain at most 12 unique YYYY-MM values.',
                status.HTTP_400_BAD_REQUEST,
            )
        if any(not isinstance(period, str) for period in periods):
            return error_response(
                request, 'VALIDATION_ERROR', 'periods must contain only YYYY-MM values.',
                status.HTTP_400_BAD_REQUEST,
            )
        if len(set(periods)) != len(periods):
            return error_response(
                request, 'VALIDATION_ERROR', 'periods must contain at most 12 unique YYYY-MM values.',
                status.HTTP_400_BAD_REQUEST,
            )
        if any(len(period) != 7 or period[4] != '-'
               or not period[:4].isdigit() or not period[5:].isdigit()
               or not 1 <= int(period[5:]) <= 12 for period in periods):
            return error_response(
                request, 'VALIDATION_ERROR', 'periods must contain only YYYY-MM values.',
                status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(expected_state, dict):
            return error_response(
                request, 'VALIDATION_ERROR', 'expectedState must be an object.',
                status.HTTP_400_BAD_REQUEST,
            )
        if set(expected_state) - {'latestRunId', 'latestSuccessAt'}:
            return error_response(
                request, 'VALIDATION_ERROR', 'expectedState contains unknown fields.',
                status.HTTP_400_BAD_REQUEST,
            )

        retry_of = None
        if retry_of_raw:
            try:
                retry_of = IngestProcessRun.objects.filter(
                    pk=uuid.UUID(str(retry_of_raw)),
                    entity=membership.entity,
                ).first()
            except ValueError:
                retry_of = None
            if (
                retry_of is None
                or retry_of.process_key != process_key
                or retry_of.state not in RETRY_SOURCE_STATES
                or not retry_of.retryable
            ):
                return error_response(
                    request, 'VALIDATION_ERROR', 'retryOfRunId is not a retryable entity run.',
                    status.HTTP_400_BAD_REQUEST,
                )

        fingerprint_payload = {
            'processKey': process_key,
            'periods': sorted(periods),
            'expectedState': expected_state,
            'retryOfRunId': str(retry_of.pk) if retry_of else None,
        }
        fingerprint = _fingerprint(fingerprint_payload)
        existing = IngestProcessRun.objects.filter(
            entity=membership.entity,
            idempotency_key=idempotency_key,
        ).select_related('actor', 'entity').first()
        if existing:
            if existing.request_fingerprint != fingerprint:
                return error_response(
                    request, 'IDEMPOTENCY_CONFLICT',
                    'The idempotency key was already used for a different request.',
                    status.HTTP_409_CONFLICT,
                )
            return Response(serialize_run(
                existing, include_idempotency=True, idempotent_replay=True,
            ), status=status.HTTP_200_OK)

        latest = IngestProcessRun.objects.filter(
            entity=membership.entity,
            process_key=process_key,
        ).order_by('-requested_at').first()
        expected_run_id = expected_state.get('latestRunId')
        if expected_run_id is not None and str(getattr(latest, 'pk', '')) != str(expected_run_id):
            return error_response(
                request, 'PRECONDITION_FAILED', 'The latest process run changed.',
                status.HTTP_409_CONFLICT,
            )
        expected_success = expected_state.get('latestSuccessAt')
        actual_success = IngestProcessRun.objects.filter(
            entity=membership.entity,
            process_key=process_key,
            state=IngestProcessRun.State.SUCCEEDED,
        ).order_by('-finished_at').values_list('finished_at', flat=True).first()
        if expected_success is not None and expected_success != _iso(actual_success):
            return error_response(
                request, 'PRECONDITION_FAILED', 'The latest successful run changed.',
                status.HTTP_409_CONFLICT,
            )

        try:
            with transaction.atomic():
                _expire_stale_active_runs(membership.entity)
                run = IngestProcessRun.objects.create(
                    entity=membership.entity,
                    actor=request.user,
                    process_key=process_key,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                    periods=sorted(periods),
                    retry_of=retry_of,
                    correlation_id=request_correlation_id(request),
                    lease_expires_at=(
                        timezone.now()
                        + timedelta(seconds=settings.WEB_API_V2_INGEST_RUN_LEASE_SECONDS)
                    ),
                )
                _replace_run_periods(run, periods)
                _audit(run, 'requested')
        except IntegrityError:
            duplicate = IngestProcessRun.objects.filter(
                entity=membership.entity,
                idempotency_key=idempotency_key,
            ).select_related('actor', 'entity').first()
            if duplicate and duplicate.request_fingerprint == fingerprint:
                return Response(serialize_run(
                    duplicate, include_idempotency=True, idempotent_replay=True,
                ), status=status.HTTP_200_OK)
            if duplicate:
                return error_response(
                    request, 'IDEMPOTENCY_CONFLICT',
                    'The idempotency key was already used for a different request.',
                    status.HTTP_409_CONFLICT,
                )
            return error_response(
                request, 'PROCESS_ALREADY_RUNNING',
                'Another ingest process is already running for this entity.',
                status.HTTP_409_CONFLICT, retryable=True,
            )

        blocker = first_blocking_prerequisite(membership.entity, process_key)
        if blocker:
            run.state = IngestProcessRun.State.BLOCKED
            run.finished_at = timezone.now()
            run.error_code = blocker['code']
            run.error_message = blocker['message']
            run.blocked_reason = {'code': blocker['code'], 'message': blocker['message']}
            run.retryable = blocker['code'] in {'DURABLE_WORKER_REQUIRED', 'XERO_DAILY_LIMIT_REACHED'}
            run.save(update_fields=(
                'state', 'finished_at', 'error_code', 'error_message',
                'blocked_reason', 'retryable',
            ))
            _audit(run, 'blocked')
            return Response(serialize_run(run, include_idempotency=True), status=status.HTTP_201_CREATED)

        run.state = IngestProcessRun.State.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=('state', 'started_at'))
        _audit(run, 'started')
        try:
            result = execute_process(process_key, membership.entity)
        except ProcessCommandError as exc:
            run.state = IngestProcessRun.State.BLOCKED if exc.blocked else IngestProcessRun.State.FAILED
            run.error_code = exc.code
            run.error_message = exc.safe_message
            run.retryable = exc.retryable
            if exc.blocked:
                run.blocked_reason = {'code': exc.code, 'message': exc.safe_message}
        except Exception as exc:
            logger.error(
                'v2_ingest_failed run=%s actor=%s entity=%s process=%s exception_type=%s correlation_id=%s',
                run.pk, request.user.pk, membership.entity_id, process_key,
                type(exc).__name__, run.correlation_id,
            )
            run.state = IngestProcessRun.State.FAILED
            run.error_code = 'PROCESS_FAILED'
            run.error_message = 'The process run failed.'
            run.retryable = True
        else:
            run.state = IngestProcessRun.State.SUCCEEDED
            run.records_summary = result['records']
            run.output_summary = result['output']
            if result['periods']:
                run.periods = result['periods']
        run.finished_at = timezone.now()
        with transaction.atomic():
            run.save(update_fields=(
                'state', 'records_summary', 'output_summary', 'periods',
                'error_code', 'error_message', 'retryable',
                'blocked_reason', 'finished_at',
            ))
            if run.state == IngestProcessRun.State.SUCCEEDED and result['periods']:
                _replace_run_periods(run, result['periods'])
            _audit(run, 'completed')
        logger.info(
            'v2_ingest_completed run=%s actor=%s entity=%s process=%s state=%s correlation_id=%s',
            run.pk, request.user.pk, membership.entity_id, process_key, run.state,
            run.correlation_id,
        )
        return Response(serialize_run(run, include_idempotency=True), status=status.HTTP_201_CREATED)


class ProcessRunDetailView(V2IngestView):
    def get(self, request, entity_id, run_id):
        membership = self._authorize(request, entity_id)
        if isinstance(membership, Response):
            return membership
        run = IngestProcessRun.objects.filter(
            pk=run_id,
            entity=membership.entity,
        ).select_related('actor', 'entity').first()
        if run is None:
            return error_response(
                request, 'RUN_NOT_FOUND', 'Process run was not found.',
                status.HTTP_404_NOT_FOUND,
            )
        return Response(serialize_run(run))


class ProcessStatusView(V2IngestView):
    def get(self, request, entity_id):
        membership = self._authorize(request, entity_id)
        if isinstance(membership, Response):
            return membership
        rows = []
        for process_key, definition in PROCESS_DEFINITIONS.items():
            latest = IngestProcessRun.objects.filter(
                entity=membership.entity,
                process_key=process_key,
            ).select_related('actor', 'entity').order_by('-requested_at').first()
            blocker = first_blocking_prerequisite(membership.entity, process_key)
            rows.append({
                'processKey': process_key,
                'displayName': definition['display_name'],
                'state': latest.state if latest else 'not_run',
                'latestRun': serialize_run(latest) if latest else None,
                'latestSuccessAt': _iso(IngestProcessRun.objects.filter(
                    entity=membership.entity,
                    process_key=process_key,
                    state=IngestProcessRun.State.SUCCEEDED,
                ).order_by('-finished_at').values_list('finished_at', flat=True).first()),
                'prerequisites': prerequisite_status(membership.entity, process_key),
                'permittedActions': [] if blocker or (
                    latest and latest.state in (IngestProcessRun.State.QUEUED, IngestProcessRun.State.RUNNING)
                ) else ['run'],
                'blockedReason': ({'code': blocker['code'], 'message': blocker['message']}
                                  if blocker else None),
            })
        return Response({'processes': rows})
