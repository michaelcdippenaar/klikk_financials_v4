"""Backfill InvestecBankAccount.owner from INVESTEC_OWNER_MAP.

DRY-RUN BY DEFAULT (matches classify_personal_expenses convention) — prints the
changes and writes nothing. Pass --commit to write. Idempotent: re-runs are
no-ops once owners are set.

    python manage.py backfill_investec_owners            # dry-run
    python manage.py backfill_investec_owners --commit
"""
from django.core.management.base import BaseCommand

from apps.investec.owner_map import backfill_owners


class Command(BaseCommand):
    help = "Set InvestecBankAccount.owner from INVESTEC_OWNER_MAP. Dry-run unless --commit."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true", help="Write owners (default: dry-run)."
        )

    def handle(self, *args, **o):
        commit = o["commit"]
        stats = backfill_owners(dry_run=not commit)
        mode = "COMMIT" if commit else "DRY-RUN"

        self.stdout.write(self.style.WARNING(
            f"[{mode}] updated={stats['updated']}  unchanged={stats['unchanged']}  "
            f"unmapped={stats['unmapped']}"
        ))
        for ch in stats["changes"]:
            if ch.get("action") == "UNMAPPED":
                self.stdout.write(self.style.ERROR(
                    f"    UNMAPPED account {ch['account_number']} — add it to INVESTEC_OWNER_MAP"
                ))
            else:
                self.stdout.write(
                    f"    {ch['account_number']:15} {ch['from']!r:30} -> {ch['to']!r}"
                )
        if not commit:
            self.stdout.write(self.style.NOTICE("  dry-run only — re-run with --commit to write."))
