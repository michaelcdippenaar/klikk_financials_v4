"""An aged stage that wrote nothing must not report success.

The aged stages are now computed from local invoices and spend no API calls,
so the daily-limit and call-budget outcomes below are no longer reachable from
this path. They are kept because _aged_result is the shared judgement for any
aged sweep, including the opt-in verify_aged_against_xero command, and because
the rule they encode — a stage that wrote nothing must not report success — is
what actually failed on 28 Aug 2026.

On 28 Aug 2026 a Standard sync finished as `succeeded` with 274 errors and zero
rows written. Aged payables and receivables were the only two stages in the
sequence whose result was never inspected — every other stage already checks
`success` or `errors` and raises. These tests close that gap and pin the rule
that a stage failure stops the sequence rather than being aggregated away.
"""
from unittest.mock import patch

from django.test import TestCase

from apps.web_api_v2.services.ingest_registry import (
    ProcessCommandError,
    execute_process,
    run_standard_sync,
)
from apps.xero.xero_core.models import XeroTenant


def _stats(**overrides):
    base = {
        'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0,
        'contact_count': 5, 'contacts_processed': 5,
        'api_calls': 5, 'stopped_early': None,
    }
    base.update(overrides)
    return base


class AgedStageTruthfulnessTests(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-truth', tenant_name='Truth Co',
        )

    def test_a_sweep_with_errors_fails_instead_of_reporting_success(self):
        with patch('apps.xero.xero_data.aged_from_invoices.sync_aged_payables_from_invoices',
                   return_value=_stats(errors=274, contacts_processed=274)):
            with self.assertRaises(ProcessCommandError) as caught:
                execute_process('aged-payables', self.tenant)

        self.assertEqual(caught.exception.code, 'PROCESS_FAILED')
        self.assertIn('274', caught.exception.safe_message)
        self.assertTrue(caught.exception.retryable)

    def test_stopping_on_the_daily_allowance_is_reported_as_blocked(self):
        with patch('apps.xero.xero_data.aged_from_invoices.sync_aged_receivables_from_invoices',
                   return_value=_stats(stopped_early='daily-limit')):
            with self.assertRaises(ProcessCommandError) as caught:
                execute_process('aged-receivables', self.tenant)

        self.assertEqual(caught.exception.code, 'XERO_DAILY_LIMIT_REACHED')
        self.assertTrue(caught.exception.blocked)

    def test_stopping_at_the_call_budget_is_not_presented_as_a_full_sweep(self):
        with patch('apps.xero.xero_data.aged_from_invoices.sync_aged_payables_from_invoices',
                   return_value=_stats(stopped_early='max-api-calls', contacts_processed=2)):
            with self.assertRaises(ProcessCommandError) as caught:
                execute_process('aged-payables', self.tenant)

        self.assertEqual(caught.exception.code, 'PROCESS_INCOMPLETE')
        self.assertIn('2 of 5', caught.exception.safe_message)

    def test_a_clean_sweep_still_succeeds(self):
        with patch('apps.xero.xero_data.aged_from_invoices.sync_aged_payables_from_invoices',
                   return_value=_stats(created=3, skipped=2)):
            result = execute_process('aged-payables', self.tenant)

        self.assertIsInstance(result, dict)

    def test_a_failing_aged_stage_fails_the_whole_standard_sync(self):
        """The headline defect: 8/8 stages 'complete' while one wrote nothing."""
        def fake_execute(process_key, entity):
            if process_key == 'aged-payables':
                raise ProcessCommandError(
                    'PROCESS_FAILED', 'Aged payables failed for 274 of 274 contacts.',
                    retryable=True,
                )
            return {'records': {}, 'periods': [], 'outputs': []}

        with patch('apps.web_api_v2.services.ingest_registry.execute_process',
                   side_effect=fake_execute):
            with self.assertRaises(ProcessCommandError) as caught:
                run_standard_sync(self.tenant)

        self.assertIn('Aged payables', caught.exception.safe_message)
        self.assertIn('274', caught.exception.safe_message)
