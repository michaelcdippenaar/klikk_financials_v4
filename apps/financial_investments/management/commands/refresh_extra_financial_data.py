"""
Refresh extra yfinance data (dividends, splits, company info, financials, earnings,
analyst recommendations, ownership, news) for one or all symbols.
"""
from django.core.management.base import BaseCommand

from apps.financial_investments.models import Symbol
from apps.financial_investments.services_extra import (
    refresh_extra_data,
    EXTRA_DATA_TYPES,
)


class Command(BaseCommand):
    help = (
        "Refresh extra yfinance data (dividends, splits, company info, financial statements, "
        "earnings, analyst recommendations, ownership, news) for symbols."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'symbol',
            nargs='?',
            type=str,
            help='Symbol to refresh (e.g. NED.JO, AAPL). If omitted, refresh all symbols.',
        )
        parser.add_argument(
            '--types',
            type=str,
            default=None,
            help=f'Comma-separated list of types to fetch. Options: {", ".join(EXTRA_DATA_TYPES)}. Default: all.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only list symbols that would be processed.',
        )

    def handle(self, *args, **options):
        symbol_str = (options.get('symbol') or '').strip()
        types_str = options.get('types')
        dry_run = options['dry_run']

        types = None
        if types_str:
            types = [t.strip() for t in types_str.split(',') if t.strip()]
            invalid = [t for t in types if t not in EXTRA_DATA_TYPES]
            if invalid:
                self.stdout.write(self.style.ERROR(f'Unknown types: {invalid}. Valid: {", ".join(EXTRA_DATA_TYPES)}'))
                return

        if symbol_str:
            symbols = [symbol_str.upper()]
        else:
            symbols = list(Symbol.objects.values_list('symbol', flat=True).order_by('symbol'))

        if not symbols:
            self.stdout.write(self.style.WARNING('No symbols to process.'))
            return

        if dry_run:
            self.stdout.write(f'Would refresh extra data for {len(symbols)} symbol(s): {", ".join(symbols[:10])}{"..." if len(symbols) > 10 else ""}')
            return

        for sym in symbols:
            self.stdout.write(f'Refreshing {sym}...')
            result = refresh_extra_data(sym, types=types)
            for k, v in result.get('results', {}).items():
                self.stdout.write(self.style.SUCCESS(f'  {k}: {v}'))
            for k, err in result.get('errors', {}).items():
                self.stdout.write(self.style.ERROR(f'  {k}: {err}'))

        self.stdout.write(self.style.SUCCESS('Done.'))
