"""Execute one queued ingest run, safely, from outside the request cycle.

The POST endpoint used to run the Xero sync inline. That meant a browser
request held a worker for the length of a full provider sync, against the
gunicorn and nginx timeouts this repository already carries three fix
documents for, and it consumed Xero API budget with no queue and no recovery.
Two production budget blowouts in August 2026 came from unbounded Xero calls.

So the endpoint now only enqueues, and this claims and runs the work.

The model was already built for it: `QUEUED` is the default state, a partial
unique constraint allows one active run per entity, and every run carries a
lease so a worker that dies is recoverable. This adds the missing executor.

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED, so several workers may run
without ever executing the same run twice.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.web_api_v2.models import IngestProcessAuditEvent, IngestProcessRun
from apps.web_api_v2.services.ingest_registry import (
    ProcessCommandError,
    execute_process,
    first_blocking_prerequisite,
)


logger = logging.getLogger(__name__)


def _audit(run, action):
    IngestProcessAuditEvent.objects.create(
        run=run, entity=run.entity, actor=run.actor, process_key=run.process_key,
        action=action, result_state=run.state, correlation_id=run.correlation_id,
        safe_metadata={'retryOfRunId': str(run.retry_of_id) if run.retry_of_id else None},
    )


def _lease_until():
    return timezone.now() + timedelta(seconds=settings.WEB_API_V2_INGEST_RUN_LEASE_SECONDS)


def reap_expired_runs():
    """Fail runs whose lease lapsed, so a dead worker cannot block an entity.

    The one-active-run-per-entity constraint means a stranded RUNNING row locks
    that entity out entirely. These are marked retryable: the work did not
    complete, and nothing here knows whether it partially applied.
    """
    reaped = 0
    with transaction.atomic():
        stale = IngestProcessRun.objects.select_for_update(skip_locked=True).filter(
            state__in=(IngestProcessRun.State.QUEUED, IngestProcessRun.State.RUNNING),
            lease_expires_at__lt=timezone.now(),
        ).select_related('actor', 'entity')
        for run in stale:
            run.state = IngestProcessRun.State.FAILED
            run.finished_at = timezone.now()
            run.error_code = 'RUN_LEASE_EXPIRED'
            run.error_message = 'The process run lease expired before completion.'
            run.retryable = True
            run.save(update_fields=(
                'state', 'finished_at', 'error_code', 'error_message', 'retryable',
            ))
            _audit(run, 'lease-expired')
            reaped += 1
    return reaped


def claim_next_run():
    """Claim the oldest queued run, or None. Skips rows another worker holds."""
    with transaction.atomic():
        run = IngestProcessRun.objects.select_for_update(skip_locked=True).filter(
            state=IngestProcessRun.State.QUEUED,
        ).select_related('actor', 'entity').order_by('requested_at').first()
        if run is None:
            return None
        run.state = IngestProcessRun.State.RUNNING
        run.started_at = timezone.now()
        run.lease_expires_at = _lease_until()
        run.save(update_fields=('state', 'started_at', 'lease_expires_at'))
        _audit(run, 'started')
        return run


def execute_claimed_run(run):
    """Run the claimed work and record a terminal state. Never raises."""
    blocker = first_blocking_prerequisite(run.entity, run.process_key)
    if blocker:
        run.state = IngestProcessRun.State.BLOCKED
        run.error_code = blocker['code']
        run.error_message = blocker['message']
        run.blocked_reason = {'code': blocker['code'], 'message': blocker['message']}
        run.retryable = blocker['code'] in {'DURABLE_WORKER_REQUIRED', 'XERO_DAILY_LIMIT_REACHED'}
        result = None
    else:
        try:
            result = execute_process(run.process_key, run.entity)
        except ProcessCommandError as exc:
            run.state = (
                IngestProcessRun.State.BLOCKED if exc.blocked
                else IngestProcessRun.State.FAILED
            )
            run.error_code = exc.code
            run.error_message = exc.safe_message
            run.retryable = exc.retryable
            if exc.blocked:
                run.blocked_reason = {'code': exc.code, 'message': exc.safe_message}
            result = None
        except Exception as exc:
            # The provider payload may carry account detail; log the type only.
            logger.error(
                'v2_ingest_worker_failed run=%s entity=%s process=%s exception_type=%s '
                'correlation_id=%s',
                run.pk, run.entity_id, run.process_key, type(exc).__name__, run.correlation_id,
            )
            run.state = IngestProcessRun.State.FAILED
            run.error_code = 'PROCESS_FAILED'
            run.error_message = 'The process run failed.'
            run.retryable = True
            result = None
        else:
            run.state = IngestProcessRun.State.SUCCEEDED
            run.records_summary = result['records']
            run.output_summary = result['output']
            if result['periods']:
                run.periods = result['periods']

    run.finished_at = timezone.now()
    run.lease_expires_at = None
    with transaction.atomic():
        run.save(update_fields=(
            'state', 'records_summary', 'output_summary', 'periods', 'error_code',
            'error_message', 'retryable', 'blocked_reason', 'finished_at', 'lease_expires_at',
        ))
        if run.state == IngestProcessRun.State.SUCCEEDED and result and result['periods']:
            from apps.web_api_v2.ingest_views import _replace_run_periods
            _replace_run_periods(run, result['periods'])
        _audit(run, 'completed')
    logger.info(
        'v2_ingest_worker_completed run=%s entity=%s process=%s state=%s correlation_id=%s',
        run.pk, run.entity_id, run.process_key, run.state, run.correlation_id,
    )
    return run
