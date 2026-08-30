"""
Invoice sync reports the months it actually wrote — fault A of the 30 Aug
blank-stage-table triad.

The V2 run record's periods are MEASURED coverage: on success the run's
requested periods are replaced with stats['affected_periods'], and the UI
never sends a claim (the portal pins that with its own scar test). The months
must come from XeroInvoice.date — the same field the stage's source evidence
is scoped by (xero_source_evidence._SOURCES) — so run evidence and source
evidence can never disagree about which clock this stage runs on. A run that
wrote nothing, or only dateless rows, claims no coverage: blank beats a
measured-looking false claim.
"""
from unittest import mock

from django.test import TestCase

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.invoices_service import sync_xero_invoices


def _payload(invoice_id, date_string):
    return {
        'InvoiceID': invoice_id,
        'InvoiceNumber': f'INV-{invoice_id}',
        'Type': 'ACCREC',
        'Status': 'AUTHORISED',
        'Date': date_string,
        'LineItems': [],
    }


class InvoiceSyncCoverageTests(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-cov', tenant_name='Coverage Co',
        )
        self.api = mock.patch(
            'apps.xero.xero_data.invoices_service._get_api', return_value=object(),
        )
        self.api.start()
        self.addCleanup(self.api.stop)

    def _run_with(self, payloads):
        with mock.patch(
            'apps.xero.xero_data.invoices_service._iter_invoice_pages',
            return_value=iter(payloads),
        ):
            return sync_xero_invoices(self.tenant)

    def test_reports_the_distinct_months_of_the_invoices_it_wrote(self):
        stats = self._run_with([
            _payload('a1', '2025-07-03'),
            _payload('a2', '2025-07-28'),
            _payload('a3', '2025-08-15'),
        ])

        self.assertEqual(stats['created'], 3)
        self.assertEqual(stats['affected_periods'], ['2025-07', '2025-08'])

    def test_a_dateless_invoice_claims_no_month(self):
        stats = self._run_with([_payload('b1', None)])

        self.assertEqual(stats['created'], 1)
        self.assertEqual(stats['affected_periods'], [])

    def test_a_failed_upsert_contributes_no_coverage(self):
        # Missing InvoiceID makes the upsert raise; the row is counted as an
        # error and must not appear in the run's measured coverage.
        broken = _payload(None, '2025-09-01')
        broken.pop('InvoiceID')
        stats = self._run_with([broken, _payload('c1', '2025-10-02')])

        self.assertEqual(stats['errors'], 1)
        self.assertEqual(stats['affected_periods'], ['2025-10'])

    def test_an_empty_run_reports_empty_coverage_not_a_claim(self):
        stats = self._run_with([])

        self.assertEqual(stats['invoice_count'], 0)
        self.assertEqual(stats['affected_periods'], [])

    def test_normalize_result_carries_the_measured_months_to_the_run_record(self):
        from apps.web_api_v2.services.ingest_registry import normalize_result

        stats = self._run_with([_payload('d1', '2026-01-09')])
        normalized = normalize_result('invoice-sync', stats)

        self.assertEqual(normalized['periods'], ['2026-01'])
