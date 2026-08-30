import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.web_api_v2.models import (
    IngestProcessAuditEvent,
    IngestProcessRun,
    IngestProcessRunPeriod,
    UserEntityMembership,
)
from apps.xero.xero_core.models import XeroTenant


SUMMARY_QUERY = '''
query Pipeline($context: FinancialContextInput!) {
  xeroPipelineSummary(context: $context) {
    context { startsOn endsOn }
    state completionPercent
    firstBlocker { stage }
    stages {
      key processKey order category required state latestRunId
      records { read }
      outputs { count }
      periodRunSummaries {
        period state latestRunId latestAttemptAt latestSuccessAt
        records { read }
        outputs { count }
        validation { state }
      }
    }
  }
}
'''

HISTORY_QUERY = '''
query PipelineHistory(
  $context: FinancialContextInput!
  $stage: XeroPipelineStageKey!
  $limit: Int!
) {
  xeroPipelineRunHistory(context: $context, stage: $stage, limit: $limit) {
    context { entityId selectedPeriods }
    stage
    runs { id stage processKey state requestedAt startedAt finishedAt periods }
  }
}
'''

DETAIL_QUERY = '''
query PipelineDetail($context: FinancialContextInput!, $runId: ID!) {
  xeroPipelineRunDetail(context: $context, runId: $runId) {
    context { entityId selectedPeriods }
    id stage processKey state requestedAt startedAt finishedAt periods
    records { read created updated skipped failed }
    outputs { code label count }
    validation { state code message }
    retryable
    safeError { code userSafeReason correlationId remediation }
  }
}
'''


@patch('apps.web_api_v2.queries.xero_pipeline.prerequisite_status', return_value=[])
@patch('apps.web_api_v2.queries.xero_pipeline.has_tenant_credentials', return_value=True)
class XeroPipelineQueryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='pipeline-reader',
            password='safe-test',
        )
        self.entity = XeroTenant.objects.create(
            tenant_id='pipeline-a',
            tenant_name='Pipeline A',
            fiscal_year_start_month=7,
        )
        self.other_entity = XeroTenant.objects.create(
            tenant_id='pipeline-b',
            tenant_name='Pipeline B',
            fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(user=self.user, entity=self.entity)
        self.url = reverse('web_api_v2:graphql')
        self.token = str(AccessToken.for_user(self.user))
        self.context = {
            'entityId': self.entity.pk,
            'financialYear': 2026,
            'periodSelection': {'mode': 'MONTHS', 'months': ['2025-07']},
        }

    def _post(self, query, variables, operation_name):
        return self.client.post(
            self.url,
            data=json.dumps({
                'query': query,
                'operationName': operation_name,
                'variables': variables,
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

    def _run(
        self,
        process_key,
        *,
        period='2025-07',
        link=True,
        state=IngestProcessRun.State.SUCCEEDED,
        idempotency_suffix='1',
        records=None,
        error_code='',
        error_message='',
        output_summary=None,
        blocked_reason=None,
        retryable=False,
    ):
        run = IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key=process_key,
            state=state,
            idempotency_key=f'{process_key}-{idempotency_suffix}',
            request_fingerprint=(idempotency_suffix[0] * 64),
            periods=[period],
            records_summary=records or {},
            output_summary=output_summary or {},
            error_code=error_code,
            error_message=error_message,
            blocked_reason=blocked_reason,
            retryable=retryable,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        if link:
            IngestProcessRunPeriod.objects.create(run=run, period=period)
        return run

    def _summary(self):
        response = self._post(
            SUMMARY_QUERY,
            {'context': self.context},
            'Pipeline',
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('errors', response.json())
        return response.json()['data']['xeroPipelineSummary']

    def test_fixed_stage_order_and_ambiguous_run_has_no_period_evidence(
        self,
        credentials,
        prerequisites,
    ):
        # A period-SCOPED stage: metadata reports its runs unscoped, so it can
        # never demonstrate a per-period lookup finding nothing.
        self._run('trail-balance', link=False)
        summary = self._summary()
        self.assertEqual(
            [stage['key'] for stage in summary['stages']],
            [
                'METADATA',
                'TRANSACTIONS_JOURNALS',
                'INVOICES',
                'PROCESS_JOURNAL_LINES',
                'TRIAL_BALANCE',
                'DOCUMENTS',
                'AGED_PAYABLES',
                'AGED_RECEIVABLES',
            ],
        )
        self.assertEqual(summary["stages"][4]["processKey"], "trail-balance")
        scoped = summary['stages'][4]['periodRunSummaries'][0]
        self.assertEqual(scoped['state'], 'NO_PERIOD_DATA')
        self.assertIsNone(scoped['latestAttemptAt'])
        self.assertIsNone(scoped['records'])
        self.assertEqual(scoped['outputs'], [])
        self.assertLess(summary['completionPercent'], 100)

    def test_newer_other_period_does_not_hide_selected_period_success(
        self,
        credentials,
        prerequisites,
    ):
        # trail-balance, not metadata: this asserts PER-PERIOD isolation, which
        # only a period-scoped stage has.
        selected = self._run('trail-balance', idempotency_suffix='a')
        other = self._run(
            'trail-balance',
            period='2025-08',
            idempotency_suffix='b',
            state=IngestProcessRun.State.FAILED,
        )
        IngestProcessRun.objects.filter(pk=other.pk).update(
            requested_at=timezone.now() + timedelta(minutes=1),
        )
        scoped = self._summary()['stages'][4]['periodRunSummaries'][0]
        self.assertEqual(scoped['state'], 'SUCCEEDED')
        self.assertEqual(scoped["latestRunId"], str(selected.pk))
        self.assertEqual(scoped['latestAttemptAt'], selected.requested_at.isoformat())

    def test_completion_requires_all_five_required_stages(
        self,
        credentials,
        prerequisites,
    ):
        for key in (
            'metadata',
            'transaction-journal-sync',
            'invoice-sync',
            'process-journals',
            'trail-balance',
        ):
            self._run(key, idempotency_suffix=key[:8])
        summary = self._summary()
        self.assertEqual(summary['completionPercent'], 100)
        self.assertEqual(summary['state'], 'COMPLETE')
        self.assertIsNone(summary['firstBlocker'])

    def test_history_is_stage_and_context_bounded(self, credentials, prerequisites):
        expected = [
            self._run('metadata', idempotency_suffix=str(index))
            for index in range(3)
        ]
        self._run('metadata', period='2025-08', idempotency_suffix='other-period')
        self._run('invoice-sync', idempotency_suffix='other-stage')
        response = self._post(
            HISTORY_QUERY,
            {'context': self.context, 'stage': 'METADATA', 'limit': 2},
            'PipelineHistory',
        )
        self.assertNotIn('errors', response.json())
        runs = response.json()['data']['xeroPipelineRunHistory']['runs']
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(run['stage'] == 'METADATA' for run in runs))
        self.assertTrue(all(run["processKey"] == "metadata" for run in runs))
        self.assertTrue(all(run['periods'] == ['2025-07'] for run in runs))
        self.assertEqual(
            {run['id'] for run in runs},
            {str(run.pk) for run in expected[-2:]},
        )

        invalid = self._post(
            HISTORY_QUERY,
            {'context': self.context, 'stage': 'METADATA', 'limit': 21},
            'PipelineHistory',
        )
        self.assertEqual(
            invalid.json()['errors'][0]['extensions']['code'],
            'VALIDATION_ERROR',
        )

    def test_detail_is_context_scoped_and_redacts_raw_values(
        self,
        credentials,
        prerequisites,
    ):
        marker = 'SECRET-MARKER-DO-NOT-RETURN'
        run = self._run(
            'metadata',
            state=IngestProcessRun.State.FAILED,
            idempotency_suffix='failed',
            records={'read': 3},
            error_code='UNKNOWN_INTERNAL_FAILURE',
            error_message=marker,
            output_summary={'token': marker, 'rawValue': marker},
            blocked_reason={'code': marker, 'message': marker},
            retryable=True,
        )
        IngestProcessAuditEvent.objects.create(
            run=run,
            entity=self.entity,
            actor=self.user,
            process_key=run.process_key,
            action='completed',
            result_state=run.state,
            correlation_id=run.correlation_id,
            safe_metadata={'payload': marker},
        )
        response = self._post(
            DETAIL_QUERY,
            {'context': self.context, 'runId': str(run.pk)},
            'PipelineDetail',
        )
        body = response.content.decode()
        self.assertNotIn(marker, body)
        detail = response.json()['data']['xeroPipelineRunDetail']
        self.assertEqual(detail["processKey"], "metadata")
        self.assertEqual(detail['safeError']['code'], 'PROCESS_FAILED')
        self.assertEqual(detail['records']['read'], 3)
        self.assertEqual(detail['outputs'][0]['count'], 3)

        other_context = {
            **self.context,
            'entityId': self.other_entity.pk,
        }
        hidden = self._post(
            DETAIL_QUERY,
            {'context': other_context, 'runId': str(run.pk)},
            'PipelineDetail',
        )
        self.assertEqual(
            hidden.json()['errors'][0]['extensions']['code'],
            'FORBIDDEN_ENTITY',
        )

    def test_missing_and_malformed_detail_errors_are_typed(
        self,
        credentials,
        prerequisites,
    ):
        missing = self._post(
            DETAIL_QUERY,
            {'context': self.context, 'runId': '00000000-0000-0000-0000-000000000001'},
            'PipelineDetail',
        )
        error = missing.json()['errors'][0]
        self.assertEqual(error['extensions']['code'], 'RUN_NOT_FOUND')
        self.assertEqual(error['extensions']['correlationId'], missing['X-Correlation-ID'])

        malformed = self._post(
            DETAIL_QUERY,
            {'context': self.context, 'runId': 'not-a-run-id'},
            'PipelineDetail',
        )
        self.assertEqual(
            malformed.json()['errors'][0]['extensions']['code'],
            'VALIDATION_ERROR',
        )
    def test_multi_period_counts_require_the_complete_selected_scope(
        self, credentials, prerequisites,
    ):
        run = self._run("metadata", link=False, records={"read": 123})
        run.periods = ["2025-07", "2025-08"]
        run.save(update_fields=("periods",))
        IngestProcessRunPeriod.objects.bulk_create([
            IngestProcessRunPeriod(run=run, period="2025-07"),
            IngestProcessRunPeriod(run=run, period="2025-08"),
        ])
        subset_stage = self._summary()["stages"][0]
        self.assertIsNone(subset_stage["records"])
        self.assertEqual(subset_stage["outputs"], [])
        self.assertIsNone(subset_stage["periodRunSummaries"][0]["records"])
        full_context = {
            **self.context,
            "periodSelection": {"mode": "MONTHS", "months": ["2025-07", "2025-08"]},
        }
        response = self._post(SUMMARY_QUERY, {"context": full_context}, "Pipeline")
        full_stage = response.json()["data"]["xeroPipelineSummary"]["stages"][0]
        self.assertEqual(full_stage["records"]["read"], 123)
        self.assertEqual(full_stage["outputs"][0]["count"], 123)
        self.assertTrue(all(item["records"] is None for item in full_stage["periodRunSummaries"]))
        all_context = {
            **self.context,
            "periodSelection": {"mode": "ALL", "months": None},
        }
        response = self._post(SUMMARY_QUERY, {"context": all_context}, "Pipeline")
        all_stage = response.json()["data"]["xeroPipelineSummary"]["stages"][0]
        self.assertEqual(all_stage["records"]["read"], 123)

    def test_a_period_agnostic_stage_reports_its_run_without_a_period(
        self, credentials, prerequisites,
    ):
        """Reported by MC: metadata ran, succeeded, and the row said "Not run".

        Metadata describes the organisation — accounts, contacts, tracking
        categories — not a month, so its runs carry no period rows and a
        per-month lookup could never see them. The stage read NO_PERIOD_DATA
        with null timestamps while two successful runs sat in the table.
        """
        run = self._run('metadata', link=False)

        metadata = self._summary()['stages'][0]
        summary = metadata['periodRunSummaries'][0]

        self.assertEqual(metadata['processKey'], 'metadata')
        self.assertEqual(summary['state'], 'SUCCEEDED')
        self.assertEqual(summary['latestRunId'], str(run.pk))
        self.assertEqual(summary['latestAttemptAt'], run.requested_at.isoformat())

    def test_a_period_agnostic_stage_reads_the_same_run_for_every_period(
        self, credentials, prerequisites,
    ):
        # One organisation-wide run, several months selected. Each month must
        # report that same run rather than one month claiming it and the rest
        # reading "not run" — the evidence is not divisible by month.
        run = self._run('metadata', link=False)

        response = self._post(SUMMARY_QUERY, {'context': {
            **self.context,
            'periodSelection': {'mode': 'MONTHS', 'months': ['2025-07', '2025-08']},
        }}, 'Pipeline')
        summaries = response.json()['data']['xeroPipelineSummary']['stages'][0]['periodRunSummaries']

        self.assertEqual(len(summaries), 2)
        for item in summaries:
            self.assertEqual(item['state'], 'SUCCEEDED')
            self.assertEqual(item['latestRunId'], str(run.pk))

    def test_a_failed_period_agnostic_run_is_not_dressed_up_as_success(
        self, credentials, prerequisites,
    ):
        # Reading unscoped must not mean reading optimistically: the newest run
        # is the answer, whatever it says.
        self._run('metadata', link=False, idempotency_suffix='ok')
        failed = self._run(
            'metadata', link=False, idempotency_suffix='bad',
            state=IngestProcessRun.State.FAILED,
        )
        IngestProcessRun.objects.filter(pk=failed.pk).update(
            requested_at=timezone.now() + timedelta(minutes=1),
        )

        summary = self._summary()['stages'][0]['periodRunSummaries'][0]

        self.assertEqual(summary['state'], 'FAILED')
        self.assertEqual(summary['latestRunId'], str(failed.pk))
        # The earlier success is still the last time it worked.
        self.assertIsNotNone(summary['latestSuccessAt'])

    def test_overlapping_multi_period_runs_do_not_double_count(
        self, credentials, prerequisites,
    ):
        first = self._run("trail-balance", link=False, idempotency_suffix="overlap-a", records={"read": 40})
        first.periods = ["2025-07", "2025-08"]
        first.save(update_fields=("periods",))
        second = self._run("trail-balance", link=False, idempotency_suffix="overlap-b", records={"read": 60})
        second.periods = ["2025-08", "2025-09"]
        second.save(update_fields=("periods",))
        IngestProcessRunPeriod.objects.bulk_create([
            IngestProcessRunPeriod(run=first, period="2025-07"),
            IngestProcessRunPeriod(run=first, period="2025-08"),
            IngestProcessRunPeriod(run=second, period="2025-08"),
            IngestProcessRunPeriod(run=second, period="2025-09"),
        ])
        context = {
            **self.context,
            "periodSelection": {
                "mode": "MONTHS",
                "months": ["2025-07", "2025-08", "2025-09"],
            },
        }
        response = self._post(SUMMARY_QUERY, {"context": context}, "Pipeline")
        stage = response.json()["data"]["xeroPipelineSummary"]["stages"][4]
        self.assertIsNone(stage["records"])
        self.assertEqual(stage["outputs"], [])
