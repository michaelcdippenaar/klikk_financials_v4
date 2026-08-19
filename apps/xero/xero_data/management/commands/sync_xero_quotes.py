"""Management command — sync Xero Quotes for one or all tenants.

Examples:
  python manage.py sync_xero_quotes --tenant-id <UUID>
  python manage.py sync_xero_quotes --tenant-id <UUID> --modified-since 2026-01-01
  python manage.py sync_xero_quotes --tenant-id <UUID> --full
  python manage.py sync_xero_quotes --all-tenants
  python manage.py sync_xero_quotes --tenant-id <UUID> --max-api-calls 150
"""
import json
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.quotes_service import sync_xero_quotes


class Command(BaseCommand):
    help = 'Sync Xero Quotes for one or all tenants.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant-id', help='Specific tenant UUID')
        parser.add_argument('--all-tenants', action='store_true',
                            help='Sync every tenant in xero_core_xerotenant')
        parser.add_argument('--modified-since',
                            help='ISO date (YYYY-MM-DD) — only sync quotes updated since')
        parser.add_argument('--full', action='store_true',
                            help='Ignore modified-since; full backfill')
        parser.add_argument('--max-api-calls', type=int, default=None,
                            help='Hard cap on Xero API calls for this run (budget guard).')

    def handle(self, *args, **opts):
        tenant_id = opts.get('tenant_id')
        all_tenants = opts.get('all_tenants')

        if not (tenant_id or all_tenants):
            raise CommandError('Provide --tenant-id or --all-tenants')

        modified_since = None
        if opts.get('modified_since'):
            try:
                modified_since = datetime.strptime(opts['modified_since'], '%Y-%m-%d')
            except ValueError as exc:
                raise CommandError(f'Bad --modified-since: {exc}')

        full = opts.get('full', False)
        max_api_calls = opts.get('max_api_calls')

        if all_tenants:
            # Skip tenants awaiting Xero re-authorization (dead refresh token).
            from apps.xero.xero_core.services import syncable_tenants
            tenants = syncable_tenants(context='sync_xero_quotes')
        else:
            try:
                tenants = [XeroTenant.objects.get(tenant_id=tenant_id)]
            except XeroTenant.DoesNotExist:
                raise CommandError(f'Tenant {tenant_id} not found')

        for tenant in tenants:
            self.stdout.write(f'Syncing quotes for {tenant.tenant_name} ({tenant.tenant_id})...')
            stats = sync_xero_quotes(tenant, modified_since=modified_since, full=full,
                                     max_api_calls=max_api_calls)
            self.stdout.write(self.style.SUCCESS(json.dumps(stats, indent=2, default=str)))
