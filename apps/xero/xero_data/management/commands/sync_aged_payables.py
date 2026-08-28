"""
Management command: sync_aged_payables

Builds Aged Payables for one or all connected tenants from the invoices
already held locally — no Xero API calls — and upserts the results into xero_data_agedpayable.

Usage:
    python manage.py sync_aged_payables
    python manage.py sync_aged_payables --tenant-id <UUID>
    python manage.py sync_aged_payables --date 2025-04-30
    python manage.py sync_aged_payables --from-xero   # verification only; hundreds of API calls
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.aged_from_invoices import sync_aged_payables_from_invoices
from apps.xero.xero_data.aged_reports_service import sync_aged_payables as sync_aged_payables_from_xero


class Command(BaseCommand):
    help = 'Sync Aged Payables By Contact from Xero into the local database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant-id',
            dest='tenant_id',
            default=None,
            help='Xero tenant UUID. Omit to run for all connected tenants.',
        )
        parser.add_argument(
            '--from-xero',
            action='store_true',
            help=(
                'Call Xero per contact instead of computing from local invoices. '
                'Xero has no bulk aged endpoint, so this is one API call per '
                'contact in the ledger — hundreds on a real tenant. Reserved for '
                'verifying the local computation.'
            ),
        )
        parser.add_argument(
            '--date',
            dest='report_date',
            default=None,
            help='Report "as at" date in YYYY-MM-DD format. Defaults to today.',
        )

    def handle(self, *args, **options):
        tenant_id = options.get('tenant_id')
        date_str = options.get('report_date')

        # Parse optional date
        report_date = None
        if date_str:
            try:
                report_date = date.fromisoformat(date_str)
            except ValueError:
                raise CommandError(f'Invalid date format: {date_str!r}. Use YYYY-MM-DD.')

        # Resolve tenants
        if tenant_id:
            try:
                tenants = [XeroTenant.objects.get(tenant_id=tenant_id)]
            except XeroTenant.DoesNotExist:
                raise CommandError(f'Tenant not found: {tenant_id}')
        else:
            # Skip tenants awaiting Xero re-authorization (dead refresh token).
            from apps.xero.xero_core.services import syncable_tenants
            tenants = syncable_tenants(context='sync_aged_payables')
            if not tenants:
                self.stdout.write(self.style.WARNING('No tenants found.'))
                return

        for tenant in tenants:
            self.stdout.write(f'Syncing aged payables for tenant: {tenant.tenant_name} ({tenant.tenant_id})')
            try:
                result = (
                    sync_aged_payables_from_xero(tenant, report_date=report_date)
                    if options.get('from_xero') else
                    sync_aged_payables_from_invoices(tenant, report_date=report_date)
                )
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(f'  ERROR: {exc}')
                )
                continue

            self.stdout.write(
                self.style.SUCCESS(
                    f'  Done — contacts: {result.get("contact_count", "?")} | '
                    f'created: {result["created"]} | '
                    f'updated: {result["updated"]} | '
                    f'skipped: {result["skipped"]} | '
                    f'errors: {result["errors"]} | '
                    f'completed_at: {result.get("completed_at", "?")}'
                )
            )
