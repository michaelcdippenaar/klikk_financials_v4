"""
Sync documents (attachments) from Xero for a tenant and link them to transactions.

Requires OAuth scope: accounting.attachments or accounting.attachments.read.

Usage:
  python manage.py sync_xero_documents TENANT_ID
  python manage.py sync_xero_documents TENANT_ID --transaction-ids ID1 ID2
  python manage.py sync_xero_documents TENANT_ID --types Invoice CreditNote
"""
from django.core.management.base import BaseCommand
from apps.xero.xero_data.document_sync import sync_documents_for_tenant


class Command(BaseCommand):
    help = 'Import documents from Xero and link them to transactions (Invoice, CreditNote, BankTransaction).'

    def add_arguments(self, parser):
        parser.add_argument(
            'tenant_id',
            type=str,
            help='Xero tenant ID to sync documents for',
        )
        parser.add_argument(
            '--transaction-ids',
            nargs='*',
            type=str,
            default=None,
            help='Optional list of Xero transaction IDs (e.g. InvoiceID). If omitted, syncs all supported transactions.',
        )
        parser.add_argument(
            '--types',
            nargs='*',
            type=str,
            default=None,
            choices=['Invoice', 'CreditNote', 'BankTransaction'],
            help='Optional transaction types to sync. Default: all supported.',
        )

    def handle(self, *args, **options):
        tenant_id = options['tenant_id']
        transaction_ids = options.get('transaction_ids')
        source_types = options.get('types')

        if transaction_ids is not None and len(transaction_ids) == 0:
            transaction_ids = None

        result = sync_documents_for_tenant(
            tenant_id,
            user=None,
            transaction_ids=transaction_ids,
            source_types=source_types,
        )

        if result['success']:
            self.stdout.write(
                self.style.SUCCESS(result['message'])
            )
        else:
            self.stdout.write(self.style.WARNING(result['message']))
        self.stdout.write(f"Synced: {result['synced']}, Skipped: {result['skipped']}")
        for err in result['errors']:
            self.stderr.write(self.style.ERROR(err))

        if result['errors']:
            raise SystemExit(1)
