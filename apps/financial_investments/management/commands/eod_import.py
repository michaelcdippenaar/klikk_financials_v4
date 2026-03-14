"""
EOD Historical Data operations: import exchange tickers, fetch fundamentals, fetch prices.

Usage:
  # List all tickers on the JSE (dry run)
  python manage.py eod_import tickers --exchange JSE --dry-run

  # Import all JSE tickers into Symbol table
  python manage.py eod_import tickers --exchange JSE

  # Fetch fundamentals (P/E, dividend yield, sector) for all symbols
  python manage.py eod_import fundamentals

  # Fetch fundamentals for one symbol
  python manage.py eod_import fundamentals --symbol NED.JO

  # Fetch EOD prices for all symbols
  python manage.py eod_import prices

  # Full pipeline: import JSE tickers + fetch fundamentals
  python manage.py eod_import full --exchange JSE
"""
import time

from django.core.management.base import BaseCommand

from apps.financial_investments.models import Symbol


class Command(BaseCommand):
    help = "EOD Historical Data: import exchange tickers, fetch fundamentals, prices, or dividends."

    def add_arguments(self, parser):
        parser.add_argument(
            'action',
            type=str,
            choices=['tickers', 'fundamentals', 'prices', 'dividends', 'full'],
            help='Action: tickers (import exchange list), fundamentals, prices, dividends, or full (tickers + fundamentals).',
        )
        parser.add_argument(
            '--exchange',
            type=str,
            default='JSE',
            help='Exchange code (default: JSE). Others: LSE, NASDAQ, NYSE, etc.',
        )
        parser.add_argument(
            '--symbol',
            type=str,
            default=None,
            help='Process a single symbol (e.g. NED.JO). If omitted, process all.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview only, do not write to database.',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=0.3,
            help='Delay between API calls in seconds (default: 0.3).',
        )

    def handle(self, *args, **options):
        action = options['action']
        exchange = options['exchange']
        symbol = options.get('symbol')
        dry_run = options['dry_run']
        delay = options['delay']

        from apps.financial_investments.services_eod import _get_api_key
        api_key = _get_api_key()
        if not api_key:
            self.stdout.write(self.style.ERROR(
                'EOD_API_KEY not configured. Set it in .env or environment.\n'
                'Get a key at https://eodhd.com/'
            ))
            return

        if action == 'tickers':
            self._import_tickers(exchange, dry_run)
        elif action == 'fundamentals':
            self._fetch_fundamentals(symbol, delay)
        elif action == 'prices':
            self._fetch_prices(symbol, delay)
        elif action == 'dividends':
            self._fetch_dividends(symbol, delay)
        elif action == 'full':
            self._import_tickers(exchange, dry_run)
            if not dry_run:
                self._fetch_fundamentals(symbol, delay)

    def _import_tickers(self, exchange, dry_run):
        from apps.financial_investments.services_eod import import_exchange_tickers

        self.stdout.write(f'Fetching ticker list for exchange: {exchange}...')
        result = import_exchange_tickers(exchange, dry_run=dry_run)

        if result.get('error'):
            self.stdout.write(self.style.ERROR(f'Error: {result["error"]}'))
            return

        self.stdout.write(f'Total on exchange: {result.get("total_on_exchange", 0)}')
        self.stdout.write(f'Already in DB: {result.get("skipped", 0)}')

        if dry_run:
            self.stdout.write(f'Would create: {result.get("created", 0)}')
            for item in result.get('imported', [])[:20]:
                self.stdout.write(f'  {item["symbol"]} — {item["name"]} ({item["type"]})')
            if result.get('created', 0) > 20:
                self.stdout.write(f'  ... and {result["created"] - 20} more')
        else:
            self.stdout.write(self.style.SUCCESS(f'Created: {result.get("created", 0)} new symbols'))

    def _fetch_fundamentals(self, symbol, delay):
        from apps.financial_investments.services_eod import fetch_fundamentals

        symbols = self._get_symbols(symbol)
        self.stdout.write(f'Fetching fundamentals for {len(symbols)} symbol(s)...')
        ok = err = 0
        for sym in symbols:
            result = fetch_fundamentals(sym)
            if result.get('error'):
                self.stdout.write(self.style.WARNING(f'  {sym}: {result["error"]}'))
                err += 1
            else:
                self.stdout.write(f'  {sym}: updated ({len(result.get("fields", []))} fields)')
                ok += 1
            time.sleep(delay)
        self.stdout.write(self.style.SUCCESS(f'Fundamentals: {ok} ok, {err} errors'))

    def _fetch_prices(self, symbol, delay):
        from apps.financial_investments.services_eod import fetch_eod_prices

        symbols = self._get_symbols(symbol)
        self.stdout.write(f'Fetching EOD prices for {len(symbols)} symbol(s)...')
        ok = err = 0
        for sym in symbols:
            result = fetch_eod_prices(sym)
            if result.get('error'):
                self.stdout.write(self.style.WARNING(f'  {sym}: {result["error"]}'))
                err += 1
            else:
                self.stdout.write(f'  {sym}: {result.get("created", 0)} price points')
                ok += 1
            time.sleep(delay)
        self.stdout.write(self.style.SUCCESS(f'Prices: {ok} ok, {err} errors'))

    def _fetch_dividends(self, symbol, delay):
        from apps.financial_investments.services_eod import fetch_dividends_eod

        symbols = self._get_symbols(symbol)
        self.stdout.write(f'Fetching EOD dividends for {len(symbols)} symbol(s)...')
        ok = err = 0
        for sym in symbols:
            result = fetch_dividends_eod(sym)
            if result.get('error'):
                self.stdout.write(self.style.WARNING(f'  {sym}: {result["error"]}'))
                err += 1
            else:
                self.stdout.write(f'  {sym}: {result.get("created", 0)} dividends')
                ok += 1
            time.sleep(delay)
        self.stdout.write(self.style.SUCCESS(f'Dividends: {ok} ok, {err} errors'))

    def _get_symbols(self, symbol):
        if symbol:
            return [symbol.strip().upper()]
        return list(Symbol.objects.values_list('symbol', flat=True).order_by('symbol'))
