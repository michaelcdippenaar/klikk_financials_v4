"""
Sync Invoices without a cursor is incremental, not a full re-pull.

Before 2026-09-03 the console button, the V2 stage and the MCP tool all
omitted `modified_since`, and the service took None to mean "everything":
38 Xero calls and ~45 s to rewrite 3,700 unchanged Klikk invoices for four
that had changed. The default is now the last successful run's stamp minus
an overlap; a tenant never synced still gets a full pull, and full=True
remains the explicit escape hatch (deletions do not surface through
If-Modified-Since).
"""
import datetime as dt
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.invoices_service import (
    DEFAULT_CURSOR_OVERLAP, default_modified_since, sync_xero_invoices,
)
from apps.xero.xero_sync.models import XeroLastUpdate


class InvoiceDefaultCursorTests(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(tenant_id='tenant-cur', tenant_name='Cursor Co')
        api = mock.patch('apps.xero.xero_data.invoices_service._get_api', return_value=object())
        api.start()
        self.addCleanup(api.stop)

    def _run(self, **kwargs):
        with mock.patch('apps.xero.xero_data.invoices_service._iter_invoice_pages',
                        return_value=iter([])) as pages:
            sync_xero_invoices(self.tenant, **kwargs)
        return pages.call_args.kwargs['modified_since']

    def _stamp(self, when):
        XeroLastUpdate.objects.update_or_create(
            end_point='invoice_store', organisation=self.tenant, defaults={'date': when})

    def test_never_synced_tenant_pulls_everything(self):
        self.assertIsNone(default_modified_since(self.tenant))
        self.assertIsNone(self._run())

    def test_default_is_last_run_minus_overlap_as_naive_utc(self):
        stamp = timezone.now().replace(microsecond=0)
        self._stamp(stamp)

        cursor = self._run()

        self.assertIsNone(cursor.tzinfo, 'Xero If-Modified-Since wants a naive UTC value')
        expected = stamp.astimezone(dt.timezone.utc).replace(tzinfo=None) - DEFAULT_CURSOR_OVERLAP
        self.assertEqual(cursor, expected)

    def test_full_flag_still_pulls_everything(self):
        self._stamp(timezone.now())
        self.assertIsNone(self._run(full=True))

    def test_an_explicit_cursor_wins_over_the_default(self):
        self._stamp(timezone.now())
        explicit = dt.datetime(2026, 1, 1)
        self.assertEqual(self._run(modified_since=explicit), explicit)

    def test_a_successful_run_advances_the_stamp_the_next_default_reads(self):
        self._run()  # no stamp yet -> full; on success the store is stamped
        self.assertIsNotNone(default_modified_since(self.tenant))
