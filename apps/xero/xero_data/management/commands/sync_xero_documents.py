"""
Sync documents (attachments) from Xero for a tenant and link them to transactions.

Requires OAuth scope: accounting.attachments or accounting.attachments.read.

Two passes (default = discovery then fetch):
  DISCOVERY  pages Invoices/BankTransactions/CreditNotes at pageSize=1000 and records
             HasAttachments per row (~1 call per 1,000 records; never summaryOnly).
  FETCH      downloads attachments only for rows flagged has_attachments=true that have
             no stored document (or were updated since --since/--modified-after).

Usage:
  python manage.py sync_xero_documents TENANT_ID                          # discover (full) + fetch backlog, newest first
  python manage.py sync_xero_documents TENANT_ID --since 3                # incremental: If-Modified-Since discovery + fetch
  python manage.py sync_xero_documents TENANT_ID --discover-only          # discovery pass only (~19 calls for Klikk)
  python manage.py sync_xero_documents TENANT_ID --no-discover --max-api-calls 3000 --headroom 1500
  python manage.py sync_xero_documents TENANT_ID --transaction-ids ID1 ID2  # explicit rows (ignores flag); also the pre-flight probe
  python manage.py sync_xero_documents TENANT_ID --probe                  # ONE list call; prints X-DayLimit-Remaining
  python manage.py sync_xero_documents TENANT_ID --validate-associations 200   # HasAttachments vs Files API Associations/Count
  python manage.py sync_xero_documents TENANT_ID --legacy-probe-all       # old behaviour: Attachments call per row

Exit codes: 0 ok; 1 per-item errors (run still completed); 3 stopped for budget/auth
(daily limit, headroom floor, re-auth required) — wrappers should stand down for the night.
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.xero.xero_core.exceptions import DailyLimitReached, TenantReauthRequired
from apps.xero.xero_data.document_sync import (
    DEFAULT_HEADROOM,
    discover_attachments_for_tenant,
    sync_documents_for_tenant,
    validate_has_attachments_against_files_api,
)

EXIT_ITEM_ERRORS = 1
EXIT_BUDGET_OR_AUTH = 3


class Command(BaseCommand):
    help = ('Import documents from Xero and link them to transactions (Invoice, CreditNote, BankTransaction). '
            'Discovery via the HasAttachments list field, fetch only for flagged rows.')

    def add_arguments(self, parser):
        parser.add_argument('tenant_id', type=str, help='Xero tenant ID to sync documents for')
        parser.add_argument(
            '--transaction-ids', nargs='*', type=str, default=None,
            help='Explicit Xero transaction IDs (e.g. InvoiceID) to fetch regardless of the '
                 'has_attachments flag. Skips discovery.',
        )
        parser.add_argument(
            '--types', nargs='*', type=str, default=None,
            choices=['Invoice', 'CreditNote', 'BankTransaction'],
            help='Transaction types to cover. Default: all supported.',
        )
        parser.add_argument(
            '--since', type=int, default=None, metavar='DAYS',
            help='Incremental mode: discovery uses If-Modified-Since=now-DAYS and the fetch only '
                 'visits flagged rows whose UpdatedDateUTC is within the window.',
        )
        parser.add_argument(
            '--modified-after', type=str, default=None, metavar='ISO_DATETIME',
            help='Incremental mode with an explicit watermark (e.g. 2026-08-01T00:00:00Z). Naive values are UTC.',
        )
        parser.add_argument(
            '--max-api-calls', type=int, default=None,
            help='Stop the FETCH pass cleanly after this many Xero API calls (tenant limits: 60/min, 5000/day). '
                 'Discovery calls are counted separately and reported.',
        )
        parser.add_argument(
            '--headroom', type=int, default=DEFAULT_HEADROOM,
            help=f'Stop cleanly once X-DayLimit-Remaining drops below this (default {DEFAULT_HEADROOM}; 0 disables).',
        )
        parser.add_argument(
            '--offset', type=int, default=0,
            help='Skip the first N candidate rows (legacy resume point; rarely needed now that fetched rows drop out).',
        )
        parser.add_argument('--discover-only', action='store_true', help='Run the discovery pass only.')
        parser.add_argument('--no-discover', action='store_true', help='Skip discovery; fetch flagged rows only.')
        parser.add_argument(
            '--no-seed', action='store_true',
            help='Do not pre-seed has_attachments from the stored collection JSON before discovery.',
        )
        parser.add_argument(
            '--probe', action='store_true',
            help='Pre-flight: exactly ONE list call (CreditNotes page 1, pageSize 1); prints X-DayLimit-Remaining.',
        )
        parser.add_argument(
            '--validate-associations', type=int, default=None, metavar='N',
            help='Compare HasAttachments with Files API GET /Associations/Count on N sampled IDs, then exit.',
        )
        parser.add_argument(
            '--legacy-probe-all', action='store_true',
            help='Old behaviour: call the Attachments endpoint for EVERY row regardless of the flag (expensive).',
        )

    # ------------------------------------------------------------------ helpers
    def _watermark(self, options):
        if options.get('since') is not None and options.get('modified_after'):
            raise CommandError('--since and --modified-after are mutually exclusive.')
        if options.get('since') is not None:
            return timezone.now() - timedelta(days=options['since'])
        if options.get('modified_after'):
            raw = options['modified_after']
            try:
                dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except ValueError:
                raise CommandError(f'Invalid --modified-after value: {raw!r} (expected ISO-8601)')
            return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)
        return None

    def _budget_abort(self, what, exc):
        self.stderr.write(self.style.ERROR(f'{what}: {exc}'))
        raise SystemExit(EXIT_BUDGET_OR_AUTH)

    # ------------------------------------------------------------------ handle
    def handle(self, *args, **options):
        tenant_id = options['tenant_id']
        transaction_ids = options.get('transaction_ids')
        source_types = options.get('types')
        headroom = options.get('headroom') or None
        if transaction_ids is not None and len(transaction_ids) == 0:
            transaction_ids = None
        modified_after = self._watermark(options)

        try:
            if options.get('probe'):
                return self._probe(tenant_id, source_types)
            if options.get('validate_associations'):
                return self._validate(tenant_id, options['validate_associations'], source_types)

            discovery = None
            run_discovery = (not options.get('no_discover') and not options.get('legacy_probe_all')
                             and transaction_ids is None)
            if run_discovery:
                discovery = discover_attachments_for_tenant(
                    tenant_id, source_types=source_types, modified_after=modified_after,
                    headroom=headroom, seed_from_collection=not options.get('no_seed'),
                )
                style = self.style.SUCCESS if discovery['success'] else self.style.WARNING
                self.stdout.write(style(discovery['message']))
                for t, per in discovery['per_type'].items():
                    self.stdout.write(f"  {t}: {per['records']} records / {per['api_calls']} call(s); "
                                      f"true={per['true']} false={per['false']} updated={per['updated']} "
                                      f"unknown_local={per['unknown_local']}")
                for err in discovery['errors']:
                    self.stderr.write(self.style.ERROR(err))
                if discovery['stopped_early'] in ('daily-limit', 'headroom-floor'):
                    self.stderr.write(self.style.ERROR(
                        f"Discovery stopped early ({discovery['stopped_early']}); skipping fetch pass."))
                    raise SystemExit(EXIT_BUDGET_OR_AUTH)
                if not discovery['success'] and 're-authorization' in discovery['message']:
                    raise SystemExit(EXIT_BUDGET_OR_AUTH)
                if options.get('discover_only'):
                    raise SystemExit(0 if discovery['success'] else EXIT_ITEM_ERRORS)

            result = sync_documents_for_tenant(
                tenant_id,
                user=None,
                transaction_ids=transaction_ids,
                source_types=source_types,
                modified_after=modified_after,
                max_api_calls=options.get('max_api_calls'),
                offset=options.get('offset') or 0,
                only_flagged=not options.get('legacy_probe_all'),
                headroom=headroom,
            )
        except DailyLimitReached as e:
            # Daily-limit hits inside the loops become stopped_early; this catches
            # ones raised outside them (e.g. client setup). Abort cleanly.
            self._budget_abort('Xero daily API limit reached', e)
        except TenantReauthRequired as e:
            self._budget_abort('Xero tenant needs re-authorization', e)

        style = self.style.SUCCESS if result['success'] else self.style.WARNING
        self.stdout.write(style(result['message']))
        total_calls = result['api_calls'] + (discovery['api_calls'] if discovery else 0)
        self.stdout.write(
            f"Synced: {result['synced']}, Skipped: {result['skipped']}, "
            f"Processed: {result['processed']}, Candidates: {result['candidates']}, "
            f"Remaining: {result['remaining']}, API calls: {result['api_calls']}"
            f" (discovery {discovery['api_calls'] if discovery else 0}, total {total_calls})"
            f", DayLimit-Remaining: {result['day_limit_remaining']}"
        )
        if result['stopped_early']:
            self.stdout.write(self.style.WARNING(
                f"Stopped early ({result['stopped_early']}). Remaining flagged rows: {result['remaining']}"
            ))
        for err in result['errors']:
            self.stderr.write(self.style.ERROR(err))

        if not result['success'] and 're-authorization' in result['message']:
            raise SystemExit(EXIT_BUDGET_OR_AUTH)
        if result['stopped_early'] in ('daily-limit', 'headroom-floor'):
            raise SystemExit(EXIT_BUDGET_OR_AUTH)
        if result['errors']:
            raise SystemExit(EXIT_ITEM_ERRORS)

    def _probe(self, tenant_id, source_types):
        """One list call (CreditNotes is the smallest collection) to confirm auth/tenant/headroom."""
        res = discover_attachments_for_tenant(
            tenant_id, source_types=['CreditNote'], page_size=1, max_api_calls=1,
            seed_from_collection=False,
        )
        if res['success']:
            self.stdout.write(self.style.SUCCESS(
                f"probe OK: 1 call; X-DayLimit-Remaining={res['day_limit_remaining']}"))
            return
        self.stderr.write(self.style.ERROR(f"probe FAILED: {res['message']} {res['errors']}"))
        raise SystemExit(EXIT_BUDGET_OR_AUTH)

    def _validate(self, tenant_id, n, source_types):
        res = validate_has_attachments_against_files_api(tenant_id, sample_size=n, source_types=source_types)
        style = self.style.SUCCESS if res['success'] else self.style.WARNING
        self.stdout.write(style(res['message']))
        for d in res['details'][:50]:
            self.stdout.write(f"  {d}")
        if res.get('scope_error'):
            raise SystemExit(EXIT_ITEM_ERRORS)
        raise SystemExit(0 if res['success'] else EXIT_ITEM_ERRORS)
