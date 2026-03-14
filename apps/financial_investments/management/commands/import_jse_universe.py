"""
Import the full JSE (Johannesburg Stock Exchange) universe into financial_investments.

Fetches all JSE-listed tickers via yfinance validation, creates Symbol records,
and optionally fetches company info (P/E, dividend yield, sector, etc.).

Sources for JSE tickers:
  1. Wikipedia "List of companies listed on the Johannesburg Stock Exchange"
  2. Fallback: brute-force common JSE codes with .JO suffix via yfinance

Usage:
  python manage.py import_jse_universe                  # import + fetch company info
  python manage.py import_jse_universe --no-info         # import symbols only, skip info fetch
  python manage.py import_jse_universe --dry-run         # preview only
  python manage.py import_jse_universe --source wiki     # Wikipedia scrape (default)
  python manage.py import_jse_universe --source file --file jse_tickers.txt  # from text file
"""
import time

from django.core.management.base import BaseCommand

from apps.financial_investments.models import Symbol


def _fetch_jse_tickers_wikipedia():
    """Scrape JSE ticker codes from Wikipedia."""
    import requests
    from html.parser import HTMLParser

    urls = [
        "https://en.wikipedia.org/wiki/List_of_companies_listed_on_the_Johannesburg_Stock_Exchange",
    ]

    tickers = set()

    class JSETableParser(HTMLParser):
        """Extract ticker codes from wiki table cells."""
        def __init__(self):
            super().__init__()
            self._in_td = False
            self._col = 0
            self._row_data = []

        def handle_starttag(self, tag, attrs):
            if tag == 'td':
                self._in_td = True
            elif tag == 'tr':
                self._col = 0
                self._row_data = []

        def handle_endtag(self, tag):
            if tag == 'td':
                self._in_td = False
                self._col += 1

        def handle_data(self, data):
            if self._in_td:
                self._row_data.append(data.strip())

    for url in urls:
        try:
            resp = requests.get(url, timeout=30, headers={
                'User-Agent': 'KlikkFinancials/1.0 (stock-data-import)'
            })
            resp.raise_for_status()

            # Extract potential ticker codes: 2-5 uppercase letters
            import re
            # Look for JSE ticker patterns in table cells
            # JSE codes are typically 3-letter codes in tables
            tables = re.findall(r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>(.*?)</table>',
                                resp.text, re.DOTALL)
            for table in tables:
                rows = re.findall(r'<tr>(.*?)</tr>', table, re.DOTALL)
                for row in rows:
                    cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    for cell in cells:
                        # Strip HTML tags
                        clean = re.sub(r'<[^>]+>', '', cell).strip()
                        # JSE codes: 2-5 uppercase letters (no numbers, no spaces)
                        if re.match(r'^[A-Z]{2,5}$', clean):
                            tickers.add(clean)
        except Exception:
            continue

    return sorted(tickers)


def _fetch_jse_tickers_from_file(path):
    """Read tickers from a text file (one per line)."""
    tickers = set()
    with open(path) as f:
        for line in f:
            code = line.strip().upper()
            if code and not code.startswith('#'):
                # Strip .JO suffix if present
                if code.endswith('.JO'):
                    code = code[:-3]
                tickers.add(code)
    return sorted(tickers)


# Well-known JSE Top 40 + broader index constituents as a reliable fallback
_JSE_KNOWN_TICKERS = [
    # Top 40
    "AGL", "AMS", "ANG", "ANH", "APN", "BHP", "BID", "BTI", "BVT", "CFR",
    "CLS", "CPI", "DSY", "EXX", "FSR", "GFI", "GLN", "GRT", "HAR", "IMP",
    "INL", "INP", "KIO", "MCG", "MNP", "MRP", "MTN", "NED", "NPN", "NRP",
    "OMU", "PRX", "REM", "RNI", "SBK", "SHP", "SLM", "SOL", "SSW", "VOD",
    # Mid-cap / broader
    "ABG", "ACL", "AEG", "AFE", "AFH", "AFT", "AIL", "AIP", "AIT", "ALT",
    "ANG", "APH", "ARI", "ARL", "AVI", "BAT", "BAW", "BCF", "BEL", "BIL",
    "BRN", "BWN", "CAT", "CCO", "CDA", "CLH", "CLW", "CMH", "CML", "COH",
    "CRG", "CSB", "CUL", "DCP", "DGH", "DIB", "DIS", "DRD", "DTC", "EMI",
    "EOH", "EPP", "EQU", "ETH", "FBR", "FFB", "FGL", "FPT", "GND", "GPH",
    "GRF", "HDC", "HET", "HLM", "HMN", "HUG", "HYP", "IBO", "IDQ", "ILE",
    "IPL", "ISA", "ITE", "ITU", "J200", "JSE", "KAL", "KAP", "KBO", "KST",
    "L2D", "LEW", "LHC", "LON", "LTE", "MAG", "MAS", "MCE", "MDI", "MEI",
    "MFF", "MMP", "MND", "MNY", "MOB", "MOT", "MPT", "MRF", "MSM", "MSP",
    "MTA", "MTH", "MTM", "MUR", "N91", "NAM", "NEP", "NPH", "NPK", "NTC",
    "NVS", "OCE", "OCT", "OMN", "ORE", "OUT", "PAN", "PAP", "PBT", "PFG",
    "PIK", "PNC", "PPE", "PPH", "PSG", "QUA", "RBP", "RCL", "RDF", "RDI",
    "REI", "REN", "RES", "RFG", "RLO", "RMH", "RMI", "SAC", "SAP", "SAR",
    "SER", "SHF", "SHP", "SNH", "SNT", "SPG", "SPP", "SRE", "SSS", "STX",
    "SUI", "SUR", "TAH", "TBS", "TCP", "TFG", "TGA", "THA", "TKG", "TON",
    "TPC", "TRE", "TRU", "TSG", "TSH", "TXT", "UPL", "VKE", "WBO", "WHL",
    "WIN", "WKF", "YRK", "ZCI", "ZED",
    # Popular ETFs
    "STX40", "STXNDQ", "STXSWX", "STXEMG", "STXWDM", "STXRES", "STXFIN",
    "STXIND", "STXRAF", "STXQUA", "STXDIV", "SYGWD", "SYGUS", "SYGEU",
    "SYGUK", "SYGJP", "CTOP50", "PTXTEN", "GLODIV", "DBXWD", "DBXUS",
    "NFEMOM", "NFSWIX", "NFEDEF", "NFEHGE", "SMART", "PREFTX", "DIVTRX",
    "MAPPSG", "CSP500", "CSPROP", "CSEW40",
]


def _validate_yfinance_ticker(yf_symbol):
    """Check if a yfinance ticker is valid by fetching minimal info."""
    import yfinance as yf
    try:
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
        # yfinance returns info even for invalid tickers, but they lack key fields
        if info and info.get('regularMarketPrice') is not None:
            return True
        if info and info.get('previousClose') is not None:
            return True
        if info and info.get('longName'):
            return True
    except Exception:
        pass
    return False


class Command(BaseCommand):
    help = "Import JSE-listed tickers into financial_investments Symbol table and fetch company info."

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            type=str,
            default='known',
            choices=['known', 'wiki', 'file', 'eod'],
            help='Ticker source: "known" (built-in ~200), "wiki" (Wikipedia), "file" (text file), "eod" (EOD Historical Data API). Default: known.',
        )
        parser.add_argument(
            '--file',
            type=str,
            default=None,
            help='Path to text file with tickers (one per line). Required when --source=file.',
        )
        parser.add_argument(
            '--no-info',
            action='store_true',
            help='Skip fetching company info (P/E, dividend yield, etc.) after import.',
        )
        parser.add_argument(
            '--no-prices',
            action='store_true',
            help='Skip fetching price history.',
        )
        parser.add_argument(
            '--validate',
            action='store_true',
            help='Validate each ticker against yfinance before importing (slower but avoids invalid symbols).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only print which tickers would be imported.',
        )
        parser.add_argument(
            '--category',
            type=str,
            default='equity',
            help='Category for imported symbols (default: equity).',
        )

    def handle(self, *args, **options):
        source = options['source']
        dry_run = options['dry_run']
        fetch_info = not options['no_info']
        fetch_prices = not options['no_prices']
        validate = options['validate']
        category = options['category']

        # Step 1: Get ticker list
        self.stdout.write(f'Source: {source}')

        if source == 'eod':
            # Use EOD Historical Data API for the complete exchange list
            from apps.financial_investments.services_eod import import_exchange_tickers
            self.stdout.write('Fetching full ticker list from EOD Historical Data API...')
            result = import_exchange_tickers('JSE', category=category, dry_run=dry_run)
            if result.get('error'):
                self.stdout.write(self.style.ERROR(f'EOD error: {result["error"]}'))
                return
            self.stdout.write(f'Total on JSE: {result.get("total_on_exchange", 0)}')
            self.stdout.write(f'Already in DB: {result.get("skipped", 0)}')
            self.stdout.write(self.style.SUCCESS(f'{"Would create" if dry_run else "Created"}: {result.get("created", 0)}'))
            if dry_run:
                for item in result.get('imported', [])[:20]:
                    self.stdout.write(f'  {item["symbol"]} — {item["name"]}')
                return
            # After EOD import, optionally fetch info and prices
            if fetch_info:
                self._fetch_info_eod()
            return

        if source == 'file':
            file_path = options.get('file')
            if not file_path:
                self.stdout.write(self.style.ERROR('--file is required when --source=file'))
                return
            raw_tickers = _fetch_jse_tickers_from_file(file_path)
            self.stdout.write(f'Read {len(raw_tickers)} tickers from {file_path}')
        elif source == 'wiki':
            self.stdout.write('Fetching tickers from Wikipedia...')
            raw_tickers = _fetch_jse_tickers_wikipedia()
            self.stdout.write(f'Found {len(raw_tickers)} potential tickers from Wikipedia')
            # Merge with known tickers
            raw_tickers = sorted(set(raw_tickers) | set(_JSE_KNOWN_TICKERS))
            self.stdout.write(f'Combined with known list: {len(raw_tickers)} tickers')
        else:
            raw_tickers = sorted(set(_JSE_KNOWN_TICKERS))
            self.stdout.write(f'Using built-in list: {len(raw_tickers)} tickers')

        # Convert to yfinance format (.JO suffix for JSE equities)
        yf_tickers = []
        for code in raw_tickers:
            if '.' in code:
                yf_tickers.append((code, code))
            else:
                yf_tickers.append((code, f'{code}.JO'))

        # Check which already exist
        existing = set(Symbol.objects.values_list('symbol', flat=True))
        new_tickers = [(code, yf) for code, yf in yf_tickers if yf not in existing]
        already = [(code, yf) for code, yf in yf_tickers if yf in existing]

        self.stdout.write(f'Already in DB: {len(already)}, New: {len(new_tickers)}')

        if dry_run:
            for code, yf in new_tickers[:50]:
                self.stdout.write(f'  Would import: {code} -> {yf}')
            if len(new_tickers) > 50:
                self.stdout.write(f'  ... and {len(new_tickers) - 50} more')
            self.stdout.write(self.style.SUCCESS('Dry run complete.'))
            return

        # Step 2: Import new symbols
        created = 0
        skipped = 0
        for code, yf_sym in new_tickers:
            if validate:
                self.stdout.write(f'  Validating {yf_sym}...', ending='')
                if not _validate_yfinance_ticker(yf_sym):
                    self.stdout.write(self.style.WARNING(' invalid, skipping'))
                    skipped += 1
                    continue
                self.stdout.write(self.style.SUCCESS(' valid'))

            Symbol.objects.get_or_create(
                symbol=yf_sym,
                defaults={
                    'name': '',
                    'exchange': 'JNB',
                    'category': category,
                },
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} new symbols ({skipped} skipped as invalid)'
        ))

        # Step 3: Optionally fetch price data
        if fetch_prices and created > 0:
            from apps.financial_investments import services
            self.stdout.write(f'\nFetching price history for {created} new symbols...')
            ok = err = 0
            for code, yf_sym in new_tickers:
                if yf_sym not in existing:
                    result = services.fetch_and_store(yf_sym)
                    if result.get('error'):
                        self.stdout.write(self.style.WARNING(f'  {yf_sym}: {result["error"]}'))
                        err += 1
                    else:
                        n = result.get('created', 0)
                        self.stdout.write(f'  {yf_sym}: {n} price points')
                        ok += 1
                    time.sleep(0.3)  # rate-limit courtesy
            self.stdout.write(self.style.SUCCESS(f'Prices: {ok} ok, {err} errors'))

        # Step 4: Optionally fetch company info (P/E, dividend yield, sector, etc.)
        if fetch_info:
            from apps.financial_investments.services_extra import refresh_extra_data
            # Refresh company_info for ALL symbols (not just new ones, to get fresh data)
            all_symbols = list(Symbol.objects.values_list('symbol', flat=True).order_by('symbol'))
            self.stdout.write(f'\nFetching company info for {len(all_symbols)} symbols...')
            ok = err = 0
            for sym in all_symbols:
                result = refresh_extra_data(sym, types=['company_info'])
                errors = result.get('errors', {})
                if errors:
                    self.stdout.write(self.style.WARNING(f'  {sym}: {list(errors.values())[0]}'))
                    err += 1
                else:
                    self.stdout.write(f'  {sym}: info updated')
                    ok += 1
                time.sleep(0.3)  # rate-limit courtesy
            self.stdout.write(self.style.SUCCESS(f'Company info: {ok} ok, {err} errors'))

        self.stdout.write(self.style.SUCCESS('\nDone! Run investment_screen in the portal to screen stocks.'))

    def _fetch_info_eod(self):
        """Fetch fundamentals via EOD Historical Data for all symbols."""
        from apps.financial_investments.services_eod import fetch_fundamentals
        all_symbols = list(Symbol.objects.values_list('symbol', flat=True).order_by('symbol'))
        self.stdout.write(f'\nFetching EOD fundamentals for {len(all_symbols)} symbols...')
        ok = err = 0
        for sym in all_symbols:
            result = fetch_fundamentals(sym)
            if result.get('error'):
                self.stdout.write(self.style.WARNING(f'  {sym}: {result["error"]}'))
                err += 1
            else:
                self.stdout.write(f'  {sym}: updated')
                ok += 1
            time.sleep(0.3)
        self.stdout.write(self.style.SUCCESS(f'EOD fundamentals: {ok} ok, {err} errors'))
