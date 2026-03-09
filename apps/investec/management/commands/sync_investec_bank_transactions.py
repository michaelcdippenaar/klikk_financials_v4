"""
Sync Investec Private Bank accounts and transactions from the Investec API into PostgreSQL.

Uses INVESTEC_CLIENT_ID, INVESTEC_CLIENT_SECRET, INVESTEC_API_KEY (and optionally INVESTEC_BASE_URL).
Default date range: last 180 days (API default). Use --from-date / --to-date to override.
"""

from datetime import date

from django.core.management.base import BaseCommand

from apps.investec.bank_sync import run_investec_bank_sync


class Command(BaseCommand):
    help = "Sync Investec Private Bank accounts and transactions from the API into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from-date",
            type=str,
            metavar="YYYY-MM-DD",
            help="Start date for transactions (default: today - 180 days)",
        )
        parser.add_argument(
            "--to-date",
            type=str,
            metavar="YYYY-MM-DD",
            help="End date for transactions (default: today)",
        )
        parser.add_argument(
            "--include-pending",
            action="store_true",
            help="Include pending transactions in the API request",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and log only; do not write to the database",
        )
        parser.add_argument(
            "--account",
            type=str,
            metavar="ID_OR_NUMBER",
            help="Sync only this account: use account_id or account_number",
        )

    def handle(self, *args, **options):
        from_date = None
        to_date = None
        if options.get("from_date"):
            try:
                from_date = date.fromisoformat(options["from_date"])
            except ValueError:
                self.stdout.write(self.style.ERROR(f"Invalid --from-date: {options['from_date']}"))
                return
        if options.get("to_date"):
            try:
                to_date = date.fromisoformat(options["to_date"])
            except ValueError:
                self.stdout.write(self.style.ERROR(f"Invalid --to-date: {options['to_date']}"))
                return

        result = run_investec_bank_sync(
            from_date=from_date,
            to_date=to_date,
            include_pending=options.get("include_pending", False),
            account_filter=(options.get("account") or "").strip() or None,
            dry_run=options.get("dry_run", False),
            update_sync_log=not options.get("dry_run", False),
        )

        if result.get("error"):
            self.stdout.write(self.style.ERROR(f"Sync failed: {result['error']}"))
            return

        if options.get("dry_run"):
            self.stdout.write(self.style.WARNING("DRY RUN – no database writes"))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Sync complete: {result['created']} created, {result['updated']} updated"
                )
            )
            if result.get("last_synced_at"):
                self.stdout.write(self.style.SUCCESS(f"Last synced at: {result['last_synced_at']}"))
