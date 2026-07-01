"""Auto-classify InvestecBankTransaction rows from the seeded rules.

DRY-RUN BY DEFAULT — prints what it would do and writes nothing. Pass --commit
to write the classifications. Manual overrides are never reconsidered.

    python manage.py classify_personal_expenses                 # dry-run, all unclassified
    python manage.py classify_personal_expenses --commit
    python manage.py classify_personal_expenses --since 2024-01-01 --commit
    python manage.py classify_personal_expenses --reclassify --commit   # re-derive auto rows after rule edits
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.personal_expenses.services import classify_transactions


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f'Invalid date (expected YYYY-MM-DD): {value!r}')


class Command(BaseCommand):
    help = "Auto-classify Investec bank transactions from the seeded rules. Dry-run unless --commit."

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help="Write classifications (default: dry-run).")
        parser.add_argument('--since', help="Only transactions with transaction_date >= YYYY-MM-DD.")
        parser.add_argument('--until', help="Only transactions with transaction_date <= YYYY-MM-DD.")
        parser.add_argument('--account', help="Restrict to this account_id or account_number (comma-separated allowed).")
        parser.add_argument('--reclassify', action='store_true',
                            help="Re-evaluate transactions that already have a NON-manual classification.")

    def handle(self, *args, **o):
        stats = classify_transactions(
            since=_parse_date(o['since']),
            until=_parse_date(o['until']),
            account=o['account'],
            dry_run=not o['commit'],
            reclassify=o['reclassify'],
        )
        mode = 'COMMIT' if o['commit'] else 'DRY-RUN'
        self.stdout.write(self.style.WARNING(
            f"[{mode}] matched={stats['matched']:,}  unmatched={stats['unmatched']:,}  written={stats['written']:,}"))

        self.stdout.write('  by type:')
        for t, v in sorted(stats['by_type'].items(), key=lambda kv: -float(kv[1]['amount'])):
            self.stdout.write(f"    {t:<10} {v['count']:>6} txns   amount={float(v['amount']):>15,.2f}")

        self.stdout.write('  top categories:')
        for c, v in list(stats['by_category'].items())[:15]:
            self.stdout.write(f"    {c:<22} {v['count']:>6} txns   amount={float(v['amount']):>14,.2f}")

        if stats['unmatched_samples']:
            self.stdout.write('  top unmatched merchants:')
            for k, n in list(stats['unmatched_samples'].items())[:12]:
                self.stdout.write(f"    {n:>5}  {k}")

        if not o['commit']:
            self.stdout.write(self.style.NOTICE('  dry-run only — re-run with --commit to write.'))
