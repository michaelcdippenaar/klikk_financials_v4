"""The worker that made the run endpoint safe to expose.

The endpoint used to execute the Xero sync inline, so a browser request held a
connection for the length of a provider sync and spent API budget with no queue
and no recovery. Two production budget blowouts in August 2026 came from
unbounded Xero calls. The endpoint now only enqueues; these cover the executor.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.web_api_v2.models import (
    IngestProcessAuditEvent,
    IngestProcessRun,
    UserEntityMembership,
)
from apps.web_api_v2.services.ingest_execution import (
    claim_next_run,
    execute_claimed_run,
    reap_expired_runs,
)
from apps.xero.xero_core.models import XeroTenant


def _result(**overrides):
    return {'records': {'created': 4}, 'output': {}, 'periods': [], **overrides}


@patch(
    'apps.web_api_v2.services.ingest_execution.first_blocking_prerequisite',
    return_value=None,
)
class IngestWorkerTests(TestCase):
    """Prerequisites are the view's gate and are covered there; the subject
    here is the executor, so they are held clear."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='worker-actor', password='safe-test-pass-7781',
        )
        self.entity = XeroTenant.objects.create(
            tenant_id='worker-entity', tenant_name='Worker (Pty) Ltd',
        )
        self.other_entity = XeroTenant.objects.create(
            tenant_id='worker-entity-2', tenant_name='Second (Pty) Ltd',
        )
        UserEntityMembership.objects.create(
            user=self.user, entity=self.entity,
            role=UserEntityMembership.Role.VIEWER, active=True,
        )

    def _queue(self, entity=None, process_key='metadata', key='k-1'):
        return IngestProcessRun.objects.create(
            entity=entity or self.entity, actor=self.user, process_key=process_key,
            idempotency_key=key, request_fingerprint=key,
            lease_expires_at=timezone.now() + timedelta(seconds=1800),
        )

    def test_a_queued_run_is_claimed_executed_and_audited(self, _prerequisites):
        run = self._queue()
        with patch('apps.web_api_v2.services.ingest_execution.execute_process') as execute:
            execute.return_value = _result()
            execute_claimed_run(claim_next_run())

        run.refresh_from_db()
        self.assertEqual(run.state, IngestProcessRun.State.SUCCEEDED)
        self.assertEqual(run.records_summary, {'created': 4})
        self.assertIsNone(run.lease_expires_at, 'a finished run must not hold a lease')
        self.assertEqual(
            list(IngestProcessAuditEvent.objects.filter(run=run).values_list('action', flat=True)),
            ['started', 'completed'],
        )

    def test_a_second_worker_never_claims_a_run_already_claimed(self, _prerequisites):
        """Two workers must not run the same provider sync twice."""
        self._queue()
        first = claim_next_run()
        second = claim_next_run()
        self.assertIsNotNone(first)
        self.assertIsNone(second, 'a claimed run was offered to a second worker')

    def test_claiming_takes_the_oldest_run_first(self, _prerequisites):
        older = self._queue(entity=self.entity, key='older')
        newer = self._queue(entity=self.other_entity, key='newer')
        IngestProcessRun.objects.filter(pk=older.pk).update(
            requested_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(claim_next_run().pk, older.pk)
        self.assertEqual(claim_next_run().pk, newer.pk)

    def test_an_expired_lease_is_reaped_so_the_entity_is_not_locked_out(self, _prerequisites):
        """One active run per entity, so a stranded row blocks that entity."""
        run = self._queue()
        IngestProcessRun.objects.filter(pk=run.pk).update(
            state=IngestProcessRun.State.RUNNING,
            lease_expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(reap_expired_runs(), 1)

        run.refresh_from_db()
        self.assertEqual(run.state, IngestProcessRun.State.FAILED)
        self.assertEqual(run.error_code, 'RUN_LEASE_EXPIRED')
        self.assertTrue(run.retryable, 'a lapsed run must be retryable')
        # The entity is free again.
        self.assertIsNotNone(self._queue(key='after-reap'))

    def test_a_failing_process_leaves_a_terminal_retryable_run_not_an_exception(self, _prerequisites):
        run = self._queue()
        with patch('apps.web_api_v2.services.ingest_execution.execute_process') as execute:
            execute.side_effect = RuntimeError('provider blew up with 388.00 and a token')
            execute_claimed_run(claim_next_run())

        run.refresh_from_db()
        self.assertEqual(run.state, IngestProcessRun.State.FAILED)
        self.assertTrue(run.retryable)
        self.assertNotIn('388.00', run.error_message)
        self.assertNotIn('token', run.error_message)

    def test_the_command_drains_the_queue_and_stops(self, _prerequisites):
        self._queue(key='drain-1')
        with patch('apps.web_api_v2.services.ingest_execution.execute_process') as execute:
            execute.return_value = _result()
            call_command('run_ingest_worker', '--once')
        self.assertFalse(
            IngestProcessRun.objects.filter(state=IngestProcessRun.State.QUEUED).exists(),
        )

    def test_the_command_exits_cleanly_with_an_empty_queue(self, _prerequisites):
        call_command('run_ingest_worker', '--once')


class StandardSyncTests(TestCase):
    """Standard sync runs every stage, so it is the largest Xero consumer."""

    def setUp(self):
        self.entity = XeroTenant.objects.create(
            tenant_id='sync-entity', tenant_name='Sync (Pty) Ltd',
        )

    @override_settings(WEB_API_V2_INGEST_WORKER_ENABLED=False)
    def test_standard_sync_stays_blocked_where_no_worker_is_declared(self):
        from apps.web_api_v2.services.ingest_registry import (
            ProcessCommandError, execute_process,
        )
        with self.assertRaises(ProcessCommandError) as raised:
            execute_process('standard-sync', self.entity)
        self.assertTrue(raised.exception.blocked)

    @override_settings(WEB_API_V2_INGEST_WORKER_ENABLED=True)
    def test_standard_sync_stops_at_the_first_failing_stage(self):
        """Continuing past a failed journal sync would build a trial balance on
        incomplete data and then report success."""
        from apps.web_api_v2.services import ingest_registry

        calls = []

        def fake(process_key, entity):
            calls.append(process_key)
            if process_key == 'transaction-journal-sync':
                raise ingest_registry.ProcessCommandError(
                    'PROCESS_FAILED', 'Transaction sync failed.', retryable=True,
                )
            return _result()

        with patch.object(ingest_registry, 'execute_process', side_effect=fake):
            with self.assertRaises(ingest_registry.ProcessCommandError) as raised:
                ingest_registry.run_standard_sync(self.entity)

        self.assertEqual(calls, ['metadata', 'transaction-journal-sync'])
        self.assertNotIn('trail-balance', calls)
        self.assertIn('Transaction and journal sync', raised.exception.safe_message)
