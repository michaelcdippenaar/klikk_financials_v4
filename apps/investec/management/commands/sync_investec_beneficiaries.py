"""
Sync Investec beneficiaries from the Investec API into PostgreSQL.

Read-only against Investec: pulls the beneficiary list captured in Investec Online
(GET /za/pb/v1/accounts/beneficiaries) for every configured credential profile.
Never creates, edits, or pays beneficiaries.

Use --dry-run to fetch and print without writing; add --raw to also dump the raw
API payloads (useful the first time, to confirm the live response shape).
"""

import json

from django.core.management.base import BaseCommand

from apps.investec.bank_sync import run_investec_beneficiary_sync


class Command(BaseCommand):
    help = "Sync Investec beneficiaries from the API into the database (read-only against Investec)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and print only; do not write to the database",
        )
        parser.add_argument(
            "--raw",
            action="store_true",
            help="With --dry-run: print raw API payloads instead of mapped fields",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        result = run_investec_beneficiary_sync(dry_run=dry_run)

        if result.get("error"):
            self.stdout.write(self.style.ERROR(f"Sync failed: {result['error']}"))
            return

        for err in result.get("errors", []):
            self.stdout.write(self.style.WARNING(f"Warning: {err}"))

        if dry_run:
            rows = result.get("beneficiaries", [])
            self.stdout.write(self.style.WARNING(f"DRY RUN – no database writes. Fetched {len(rows)} beneficiaries."))
            for row in rows:
                if options.get("raw"):
                    self.stdout.write(json.dumps(row.get("collection"), indent=2, default=str))
                else:
                    self.stdout.write(
                        f"  [{row['source_profile']}] {row['name'] or row['beneficiary_name']:<40} "
                        f"{row['bank_name']:<20} {row['account_number']:<15} "
                        f"last paid {row['last_payment_amount'] or '-'} on {row['last_payment_date'] or '-'}"
                    )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Beneficiary sync complete ({result.get('profiles_synced', 0)} profile(s)): "
                f"{result['created']} created, {result['updated']} updated, "
                f"{result['deactivated']} deactivated"
            )
        )
