"""The scheduled aged sync must not call Xero per contact.

klikk-sync.sh runs sync_aged_receivables and sync_aged_payables for all three
tenants every Monday at 06:00. Because Xero has no bulk aged endpoint, each of
those six invocations called every contact in the ledger.

On 2026-08-24 that drove X-DayLimit-Remaining from 903 to 189 in five minutes
and logged 462 per-minute rate-limit errors — and, because the report parser
never descended into Xero's Section rows, wrote nothing at all. Every earlier
week spent the same calls more slowly and also wrote nothing.

These pin that the scheduled path now computes locally, and that reaching Xero
is possible only when someone asks for it by name.
"""
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.xero.xero_core.models import XeroTenant


class AgedCommandDefaultTests(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-cmd', tenant_name='Cron Co',
        )
        self.stats = {
            'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0,
            'contact_count': 0, 'contacts_processed': 0, 'api_calls': 0,
            'stopped_early': None, 'completed_at': 'now',
        }

    def test_the_scheduled_command_computes_locally_and_calls_no_api(self):
        for kind in ('payables', 'receivables'):
            with self.subTest(kind=kind), \
                    patch(f'apps.xero.xero_data.management.commands.sync_aged_{kind}'
                          f'.sync_aged_{kind}_from_invoices', return_value=self.stats) as local, \
                    patch(f'apps.xero.xero_data.management.commands.sync_aged_{kind}'
                          f'.sync_aged_{kind}_from_xero') as remote:
                call_command(f'sync_aged_{kind}', '--tenant-id', self.tenant.tenant_id)

                local.assert_called_once()
                remote.assert_not_called()

    def test_reaching_xero_requires_asking_for_it_by_name(self):
        for kind in ('payables', 'receivables'):
            with self.subTest(kind=kind), \
                    patch(f'apps.xero.xero_data.management.commands.sync_aged_{kind}'
                          f'.sync_aged_{kind}_from_invoices') as local, \
                    patch(f'apps.xero.xero_data.management.commands.sync_aged_{kind}'
                          f'.sync_aged_{kind}_from_xero', return_value=self.stats) as remote:
                call_command(f'sync_aged_{kind}', '--tenant-id', self.tenant.tenant_id, '--from-xero')

                remote.assert_called_once()
                local.assert_not_called()
