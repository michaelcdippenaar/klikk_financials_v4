"""Management command — sync Xero Quotes for one or all tenants.

Examples:
  python manage.py sync_xero_quotes --tenant-id <UUID>
  python manage.py sync_xero_quotes --tenant-id <UUID> --modified-since 2026-01-01
  python manage.py sync_xero_quotes --tenant-id <UUID> --full
  python manage.py sync_xero_quotes --all-tenants
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

        if all_tenants:
            tenants = XeroTenant.objects.all()
        else:
            try:
                tenants = [XeroTenant.objects.get(tenant_id=tenant_id)]
            except XeroTenant.DoesNotExist:
                raise CommandError(f'Tenant {tenant_id} not found')

        for tenant in tenants:
            self.stdout.write(f'Syncing quotes for {tenant.tenant_name} ({tenant.tenant_id})...')
            stats = sync_xero_quotes(tenant, modified_since=modified_since, full=full)
            self.stdout.write(self.style.SUCCESS(json.dumps(stats, indent=2, default=str)))
