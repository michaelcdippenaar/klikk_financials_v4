"""
Management command: sync_aged_receivables

Pulls Aged Receivables By Contact from Xero for one or all connected tenants
and upserts the results into xero_data_agedreceivable.

Usage:
    python manage.py sync_aged_receivables
    python manage.py sync_aged_receivables --tenant-id <UUID>
    python manage.py sync_aged_receivables --date 2025-04-30
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.aged_reports_service import sync_aged_receivables


class Command(BaseCommand):
    help = 'Sync Aged Receivables By Contact from Xero into the local database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant-id',
            dest='tenant_id',
            default=None,
            help='Xero tenant UUID. Omit to run for all connected tenants.',
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

        report_date = None
        if date_str:
            try:
                report_date = date.fromisoformat(date_str)
            except ValueError:
                raise CommandError(f'Invalid date format: {date_str!r}. Use YYYY-MM-DD.')

        if tenant_id:
            try:
                tenants = [XeroTenant.objects.get(tenant_id=tenant_id)]
            except XeroTenant.DoesNotExist:
                raise CommandError(f'Tenant not found: {tenant_id}')
        else:
            tenants = list(XeroTenant.objects.all())
            if not tenants:
                self.stdout.write(self.style.WARNING('No tenants found.'))
                return

        for tenant in tenants:
            self.stdout.write(f'Syncing aged receivables for tenant: {tenant.tenant_name} ({tenant.tenant_id})')
            try:
                result = sync_aged_receivables(tenant, report_date=report_date)
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
