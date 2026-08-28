import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.web_api_v2.models import (
    IngestProcessAuditEvent,
    IngestProcessRun,
    IngestProcessRunPeriod,
    UserEntityCapability,
    UserEntityMembership,
)
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_core.models import XeroTenant


@override_settings(KLIKK_API_TOKEN='service-secret')
class IngestProcessApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='operator', password='safe-test-pass-9274', first_name='Run', last_name='User',
        )
        self.other_user = get_user_model().objects.create_user(
            username='other', password='safe-test-pass-9274',
        )
        self.entity = XeroTenant.objects.create(tenant_id='entity-a', tenant_name='Entity A')
        self.other_entity = XeroTenant.objects.create(tenant_id='entity-b', tenant_name='Entity B')
        self.membership = UserEntityMembership.objects.create(user=self.user, entity=self.entity)
        self.other_membership = UserEntityMembership.objects.create(
            user=self.other_user, entity=self.other_entity,
        )
        self.grant = UserEntityCapability.objects.create(
            membership=self.membership,
            code=UserEntityCapability.Code.RUN_INGESTION_PROCESS,
            granted_by=self.user,
        )
        XeroClientCredentials.objects.create(
            user=self.user,
            client_id='test-client',
            client_secret='test-secret',
            scope=[],
            tenant_tokens={self.entity.pk: {'token': {'access_token': 'test'}}},
        )
        self.token = str(AccessToken.for_user(self.user))

    def _url(self, entity=None):
        return reverse(
            'web_api_v2_entities:ingest-process-runs',
            kwargs={'entity_id': (entity or self.entity).pk},
        )

    def _drain(self):
        """Run the queue exactly as the deployed worker does."""
        from apps.web_api_v2.services.ingest_execution import (
            claim_next_run, execute_claimed_run,
        )
        drained = 0
        while (run := claim_next_run()) is not None:
            execute_claimed_run(run)
            drained += 1
        return drained

    def _post(self, payload, *, token=None, entity=None):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token or self.token}'} if token is not False else {}
        return self.client.post(
            self._url(entity),
            data=json.dumps(payload),
            content_type='application/json',
            **headers,
        )

    @staticmethod
    def _result():
        return {
            'records': {'created': 4},
            'output': {'processKey': 'metadata', 'stats': {'accounts_updated': 4}},
            'periods': [],
        }

    def test_missing_and_service_credentials_are_rejected(self):
        payload = {'processKey': 'metadata', 'idempotencyKey': 'request-0001'}
        self.assertEqual(self._post(payload, token=False).status_code, 401)
        response = self._post(payload, token='service-secret')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'UNAUTHENTICATED')

    def test_membership_without_capability_is_denied_by_default(self):
        self.grant.delete()
        response = self._post({'processKey': 'metadata', 'idempotencyKey': 'request-0002'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'CAPABILITY_REQUIRED')
        self.assertFalse(IngestProcessRun.objects.exists())

    def test_cross_entity_access_is_denied_without_leaking_runs(self):
        response = self._post(
            {'processKey': 'metadata', 'idempotencyKey': 'request-0003'},
            entity=self.other_entity,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'FORBIDDEN_ENTITY')

    @patch('apps.web_api_v2.services.ingest_execution.execute_process')
    def test_a_run_is_queued_by_the_request_and_executed_by_the_worker(self, execute):
        """The request must not execute. It used to run the whole Xero sync
        inline, holding a connection past the proxy timeout and spending API
        budget with no queue and no recovery."""
        execute.return_value = self._result()
        response = self._post({
            'processKey': 'metadata',
            'idempotencyKey': 'request-0004',
            'periods': ['2026-07'],
        })
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body['state'], 'queued')
        self.assertIsNone(body['startedAt'])
        self.assertIsNone(body['finishedAt'])
        self.assertFalse(body['idempotentReplay'])
        execute.assert_not_called()

        self.assertEqual(self._drain(), 1)
        run = IngestProcessRun.objects.get(pk=body['id'])
        self.assertEqual(run.state, IngestProcessRun.State.SUCCEEDED)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.actor, self.user)
        self.assertEqual(run.records_summary, {'created': 4})
        self.assertEqual(
            list(IngestProcessRunPeriod.objects.filter(run=run).values_list('period', flat=True)),
            ['2026-07'],
        )
        self.assertEqual(
            list(IngestProcessAuditEvent.objects.filter(run=run).values_list('action', flat=True)),
            ['requested', 'started', 'completed'],
        )
        execute.assert_called_once_with('metadata', self.entity)

    @patch('apps.web_api_v2.services.ingest_execution.execute_process')
    def test_idempotent_replay_and_changed_fingerprint_conflict(self, execute):
        execute.return_value = self._result()
        payload = {'processKey': 'metadata', 'idempotencyKey': 'request-0005'}
        first = self._post(payload)
        replay = self._post(payload)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()['idempotentReplay'])
        self.assertEqual(first.json()['id'], replay.json()['id'])
        self.assertEqual(IngestProcessRun.objects.count(), 1)
        conflict = self._post({**payload, 'periods': ['2026-07']})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['error']['code'], 'IDEMPOTENCY_CONFLICT')

    def test_one_active_run_per_entity_prevents_concurrent_commands(self):
        IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key='metadata',
            state=IngestProcessRun.State.RUNNING,
            idempotency_key='already-running',
            request_fingerprint='a' * 64,
        )
        response = self._post({'processKey': 'invoice-sync', 'idempotencyKey': 'request-0006'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'PROCESS_ALREADY_RUNNING')

    def test_unknown_process_and_oversized_history_limit_are_typed(self):
        unknown = self._post({'processKey': 'arbitrary.module.call', 'idempotencyKey': 'request-0007'})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()['error']['code'], 'UNKNOWN_PROCESS')
        history = self.client.get(
            self._url(),
            {'limit': 51},
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )
        self.assertEqual(history.status_code, 400)
        self.assertEqual(history.json()['error']['code'], 'VALIDATION_ERROR')

    def test_malformed_period_members_return_typed_redacted_validation_errors(self):
        marker = 'qa-supplied-secret-period-value'
        cases = (
            ({'value': marker}, 'collection-object'),
            (None, 'null'),
            ('2026-07', 'string'),
            ([{'value': marker}], 'object'),
            ([[marker]], 'nested-list'),
            ([202607], 'number'),
            (['2026-13'], 'bad-month'),
            (['2026-07', '2026-07'], 'duplicate'),
        )
        for periods, suffix in cases:
            with self.subTest(suffix=suffix):
                response = self._post({
                    'processKey': 'metadata',
                    'idempotencyKey': f'malformed-{suffix}',
                    'periods': periods,
                })
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response['Content-Type'], 'application/json')
                body = response.json()
                self.assertEqual(body['error']['code'], 'VALIDATION_ERROR')
                self.assertFalse(body['error']['retryable'])
                self.assertEqual(
                    response['X-Correlation-ID'], body['error']['correlationId'],
                )
                self.assertNotIn(marker, response.content.decode())
        self.assertFalse(IngestProcessRun.objects.exists())

    def test_standard_sync_is_durably_blocked_until_worker_exists(self):
        response = self._post({'processKey': 'standard-sync', 'idempotencyKey': 'request-0008'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['state'], 'blocked')
        self.assertEqual(response.json()['blockedReason']['code'], 'DURABLE_WORKER_REQUIRED')
        self.assertEqual(response.json()['permittedActions'], [])

    @patch('apps.web_api_v2.services.ingest_execution.execute_process')
    def test_server_prerequisite_blocks_before_command_execution(self, execute):
        XeroClientCredentials.objects.all().delete()
        response = self._post({'processKey': 'metadata', 'idempotencyKey': 'request-prereq'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['state'], 'blocked')
        self.assertEqual(
            response.json()['blockedReason']['code'], 'XERO_CONNECTION_CONFIGURED',
        )
        execute.assert_not_called()

    @patch('apps.web_api_v2.services.ingest_execution.execute_process')
    def test_retry_creates_new_linked_audited_run(self, execute):
        failed = IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key='metadata',
            state=IngestProcessRun.State.FAILED,
            idempotency_key='failed-source',
            request_fingerprint='c' * 64,
            retryable=True,
            error_code='PROCESS_FAILED',
            error_message='The process run failed.',
        )
        execute.return_value = self._result()
        response = self._post({
            'processKey': 'metadata',
            'idempotencyKey': 'request-retry-1',
            'retryOfRunId': str(failed.pk),
        })
        self.assertEqual(response.status_code, 202)
        self._drain()
        self.assertEqual(response.json()['retryOfRunId'], str(failed.pk))

    @patch('apps.web_api_v2.services.ingest_execution.execute_process')
    def test_expired_active_run_is_failed_audited_and_replaced(self, execute):
        stale = IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key='metadata',
            state=IngestProcessRun.State.RUNNING,
            idempotency_key='stale-active-run',
            request_fingerprint='d' * 64,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        execute.return_value = self._result()

        response = self._post({
            'processKey': 'metadata',
            'idempotencyKey': 'request-after-stale',
        })

        self.assertEqual(response.status_code, 202)
        self._drain()
        stale.refresh_from_db()
        self.assertEqual(stale.state, IngestProcessRun.State.FAILED)
        self.assertEqual(stale.error_code, 'RUN_LEASE_EXPIRED')
        self.assertTrue(stale.retryable)
        self.assertTrue(
            IngestProcessAuditEvent.objects.filter(
                run=stale, action='lease-expired', result_state=IngestProcessRun.State.FAILED,
            ).exists(),
        )

    @patch('apps.web_api_v2.services.ingest_execution.execute_process')
    def test_safe_failure_redacts_exception_and_is_retryable(self, execute):
        marker = 'secret-token-and-financial-value-388.00'
        execute.side_effect = RuntimeError(marker)
        response = self._post({'processKey': 'metadata', 'idempotencyKey': 'request-0009'})
        self.assertEqual(response.status_code, 202)
        with self.assertLogs('apps.web_api_v2.services.ingest_execution', level='ERROR') as captured:
            self._drain()

        run = IngestProcessRun.objects.get(pk=response.json()['id'])
        self.assertEqual(run.state, IngestProcessRun.State.FAILED)
        self.assertEqual(run.error_code, 'PROCESS_FAILED')
        self.assertTrue(run.retryable)
        # The provider message may carry a token or a financial value; neither
        # the stored run nor the log may repeat it.
        self.assertNotIn(marker, run.error_message)
        self.assertNotIn(marker, response.content.decode())
        self.assertNotIn(marker, '\n'.join(captured.output))

    @patch('apps.web_api_v2.services.ingest_execution.execute_process')
    def test_precondition_and_cursor_bounded_entity_history(self, execute):
        execute.return_value = self._result()
        first = self._post({'processKey': 'metadata', 'idempotencyKey': 'request-0010'})
        self._drain()
        stale = self._post({
            'processKey': 'metadata',
            'idempotencyKey': 'request-0011',
            'expectedState': {'latestRunId': str(uuid.uuid4())},
        })
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error']['code'], 'PRECONDITION_FAILED')

        # One active run per entity is a hard constraint, so each must reach a
        # terminal state before the next is accepted.
        self._post({'processKey': 'metadata', 'idempotencyKey': 'request-0012'})
        self._drain()
        self._post({'processKey': 'metadata', 'idempotencyKey': 'request-0013'})
        self._drain()
        page_one = self.client.get(
            self._url(), {'limit': 2}, HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )
        self.assertEqual(page_one.status_code, 200)
        self.assertEqual(len(page_one.json()['results']), 2)
        self.assertIsNotNone(page_one.json()['nextCursor'])
        page_two = self.client.get(
            self._url(),
            {'limit': 2, 'cursor': page_one.json()['nextCursor']},
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )
        self.assertEqual(len(page_two.json()['results']), 1)
        self.assertEqual(first.json()['id'], page_two.json()['results'][0]['id'])

    def test_status_and_detail_are_capability_and_entity_scoped(self):
        status_url = reverse(
            'web_api_v2_entities:ingest-process-status',
            kwargs={'entity_id': self.entity.pk},
        )
        response = self.client.get(status_url, HTTP_AUTHORIZATION=f'Bearer {self.token}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['processes']), 9)
        self.assertTrue(all(item['state'] == 'not_run' for item in response.json()['processes']))
        other_status_url = reverse(
            'web_api_v2_entities:ingest-process-status',
            kwargs={'entity_id': self.other_entity.pk},
        )
        other_status = self.client.get(
            other_status_url, HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )
        self.assertEqual(other_status.status_code, 403)
        self.assertEqual(other_status.json()['error']['code'], 'FORBIDDEN_ENTITY')
        self.grant.active = False
        self.grant.save(update_fields=('active', 'updated_at'))
        denied = self.client.get(status_url, HTTP_AUTHORIZATION=f'Bearer {self.token}')
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()['error']['code'], 'CAPABILITY_REQUIRED')

    @override_settings(WEB_API_V2_INGEST_MAX_REQUEST_BYTES=8)
    def test_ingest_request_size_limit_is_typed(self):
        response = self._post({'processKey': 'metadata', 'idempotencyKey': 'request-size'})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()['error']['code'], 'VALIDATION_ERROR')
