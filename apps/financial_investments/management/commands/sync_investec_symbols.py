"""
Add all Investec JSE share codes as financial_investments symbols and fetch yfinance data.
JSE stocks use .JO suffix in yfinance (e.g. NED -> NED.JO).
"""
from django.core.management.base import BaseCommand

from apps.investec.models import InvestecJseShareNameMapping, InvestecJsePortfolio
from apps.financial_investments import services


def to_yfinance_symbol(share_code):
    """Convert Investec share_code to yfinance ticker. JSE = .JO suffix."""
    code = (share_code or '').strip().upper()
    if not code:
        return None
    # If already has exchange suffix (e.g. AAPL, MSFT or something.XX), use as-is
    if '.' in code:
        return code
    # JSE (Johannesburg) uses .JO on Yahoo/yfinance
    return f"{code}.JO"


class Command(BaseCommand):
    help = "Add all Investec JSE share codes as symbols and fetch price data from yfinance."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only print which symbols would be added, do not fetch.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        # Distinct share codes from mapping (where set)
        from_mapping = set(
            InvestecJseShareNameMapping.objects.exclude(
                share_code__isnull=True
            ).exclude(
                share_code=''
            ).values_list('share_code', flat=True)
        )
        # Distinct share codes from portfolio (in case not yet in mapping)
        from_portfolio = set(
            InvestecJsePortfolio.objects.values_list('share_code', flat=True).distinct()
        )
        all_codes = sorted(from_mapping | from_portfolio)
        yf_symbols = []
        for code in all_codes:
            sym = to_yfinance_symbol(code)
            if sym:
                yf_symbols.append((code, sym))

        if not yf_symbols:
            self.stdout.write(self.style.WARNING('No share codes found in Investec mapping or portfolio.'))
            return

        self.stdout.write(f'Found {len(yf_symbols)} share code(s) -> yfinance symbol(s):')
        for code, sym in yf_symbols:
            self.stdout.write(f'  {code} -> {sym}')

        if dry_run:
            self.stdout.write(self.style.SUCCESS('Dry run: no fetch performed.'))
            return

        ok = 0
        err = 0
        for code, yf_sym in yf_symbols:
            result = services.fetch_and_store(yf_sym)
            if result.get('error'):
                self.stdout.write(self.style.ERROR(f'  {yf_sym}: {result["error"]}'))
                err += 1
            else:
                created = result.get('created', 0)
                self.stdout.write(self.style.SUCCESS(f'  {yf_sym}: stored {created} price points'))
                ok += 1

        self.stdout.write(self.style.SUCCESS(f'Done: {ok} ok, {err} errors.'))
