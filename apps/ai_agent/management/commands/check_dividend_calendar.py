"""
Check yfinance for newly declared dividends and auto-write TM1 adjustments.

Run daily via cron:
  python manage.py check_dividend_calendar
  python manage.py check_dividend_calendar --dry-run
  python manage.py check_dividend_calendar --symbol ABG
"""
import json

from django.core.management.base import BaseCommand

from apps.ai_agent.skills.dividend_forecast import (
    _run_dividend_calendar_update,
    check_declared_dividends,
)


class Command(BaseCommand):
    help = 'Check yfinance for newly declared dividends and auto-write TM1 adjustments.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only check for dividends, do not write TM1 adjustments.',
        )
        parser.add_argument(
            '--symbol',
            type=str,
            default='',
            help='Check a specific share code only (e.g. ABG).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        symbol = options['symbol']

        if dry_run or symbol:
            self.stdout.write(f"Checking declared dividends (dry_run={dry_run}, symbol={symbol or 'all'})...")
            result = check_declared_dividends(listed_share=symbol)
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        self.stdout.write("Running full dividend calendar update (check + TM1 write)...")
        result = _run_dividend_calendar_update()
        self.stdout.write(json.dumps(result, indent=2, default=str))

        if 'error' in result:
            self.stderr.write(self.style.ERROR(f"Error: {result['error']}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done: {result.get('adjustments_written', 0)} adjustments written, "
                f"{result.get('checked', 0)} shares checked."
            ))
