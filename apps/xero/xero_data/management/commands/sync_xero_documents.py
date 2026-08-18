"""
Sync documents (attachments) from Xero for a tenant and link them to transactions.

Requires OAuth scope: accounting.attachments or accounting.attachments.read.

Usage:
  python manage.py sync_xero_documents TENANT_ID
  python manage.py sync_xero_documents TENANT_ID --transaction-ids ID1 ID2
  python manage.py sync_xero_documents TENANT_ID --types Invoice CreditNote
  python manage.py sync_xero_documents TENANT_ID --since 3                  # incremental: last 3 days
  python manage.py sync_xero_documents TENANT_ID --modified-after 2026-08-01T00:00:00Z
  python manage.py sync_xero_documents TENANT_ID --max-api-calls 4000 --offset 0   # chunked backfill
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.xero.xero_core.exceptions import DailyLimitReached
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
        parser.add_argument(
            '--since',
            type=int,
            default=None,
            metavar='DAYS',
            help='Incremental mode: only sync transactions whose Xero UpdatedDateUTC is within the last DAYS days.',
        )
        parser.add_argument(
            '--modified-after',
            type=str,
            default=None,
            metavar='ISO_DATETIME',
            help='Incremental mode: only sync transactions updated in Xero at/after this ISO-8601 datetime '
                 '(e.g. 2026-08-01T00:00:00Z). Naive values are treated as UTC.',
        )
        parser.add_argument(
            '--max-api-calls',
            type=int,
            default=None,
            help='Stop cleanly after this many Xero API calls (tenant limits: 60/min, 5000/day).',
        )
        parser.add_argument(
            '--offset',
            type=int,
            default=0,
            help='Skip the first N transactions (pk order) — resume point for a chunked backfill.',
        )

    def handle(self, *args, **options):
        tenant_id = options['tenant_id']
        transaction_ids = options.get('transaction_ids')
        source_types = options.get('types')

        if transaction_ids is not None and len(transaction_ids) == 0:
            transaction_ids = None

        if options.get('since') is not None and options.get('modified_after'):
            raise CommandError('--since and --modified-after are mutually exclusive.')

        modified_after = None
        if options.get('since') is not None:
            modified_after = timezone.now() - timedelta(days=options['since'])
        elif options.get('modified_after'):
            raw = options['modified_after']
            try:
                modified_after = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except ValueError:
                raise CommandError(f'Invalid --modified-after value: {raw!r} (expected ISO-8601)')
            if modified_after.tzinfo is None:
                modified_after = modified_after.replace(tzinfo=dt_timezone.utc)

        try:
            result = sync_documents_for_tenant(
                tenant_id,
                user=None,
                transaction_ids=transaction_ids,
                source_types=source_types,
                modified_after=modified_after,
                max_api_calls=options.get('max_api_calls'),
                offset=options.get('offset') or 0,
            )
        except DailyLimitReached as e:
            # Daily-limit hits inside the sync loop become stopped_early='daily-limit';
            # this catches ones raised outside it (e.g. client setup). Abort cleanly —
            # a non-zero exit makes the backfill wrapper stand down for the night.
            self.stderr.write(self.style.ERROR(f'Xero daily API limit reached: {e}'))
            raise SystemExit(1)

        if result['success']:
            self.stdout.write(
                self.style.SUCCESS(result['message'])
            )
        else:
            self.stdout.write(self.style.WARNING(result['message']))
        self.stdout.write(
            f"Synced: {result['synced']}, Skipped: {result['skipped']}, "
            f"Processed: {result['processed']}, API calls: {result['api_calls']}"
        )
        if result['stopped_early']:
            self.stdout.write(self.style.WARNING(
                f"Stopped early ({result['stopped_early']}). Resume with: --offset {result['next_offset']}"
            ))
        for err in result['errors']:
            self.stderr.write(self.style.ERROR(err))

        if result['errors']:
            raise SystemExit(1)
