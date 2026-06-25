"""Sync Xero Invoices (sales + bills) for one or all tenants."""
import json
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.invoices_service import sync_xero_invoices


class Command(BaseCommand):
    help = 'Sync Xero Invoices into XeroInvoice + XeroInvoiceLineItem tables.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant-id')
        parser.add_argument('--all-tenants', action='store_true')
        parser.add_argument('--modified-since')
        parser.add_argument('--type', choices=['ACCREC', 'ACCPAY'],
                            help='Filter by Xero invoice Type (omit for both)')
        parser.add_argument('--statuses', nargs='+',
                            help='e.g. AUTHORISED PAID — defaults to all')
        parser.add_argument('--full', action='store_true')

    def handle(self, *args, **opts):
        if not (opts.get('tenant_id') or opts.get('all_tenants')):
            raise CommandError('Provide --tenant-id or --all-tenants')

        modified_since = None
        if opts.get('modified_since'):
            try:
                modified_since = datetime.strptime(opts['modified_since'], '%Y-%m-%d')
            except ValueError as exc:
                raise CommandError(f'Bad --modified-since: {exc}')

        invoice_type = opts.get('type')
        statuses = opts.get('statuses')
        full = opts.get('full', False)

        if opts.get('all_tenants'):
            tenants = XeroTenant.objects.all()
        else:
            try:
                tenants = [XeroTenant.objects.get(tenant_id=opts['tenant_id'])]
            except XeroTenant.DoesNotExist:
                raise CommandError(f'Tenant {opts["tenant_id"]} not found')

        for tenant in tenants:
            self.stdout.write(f'Syncing invoices for {tenant.tenant_name}...')
            stats = sync_xero_invoices(
                tenant, modified_since=modified_since,
                statuses=statuses, invoice_type=invoice_type, full=full,
            )
            self.stdout.write(self.style.SUCCESS(json.dumps(stats, indent=2, default=str)))
