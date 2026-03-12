"""
Refresh price history and extra data for all tracked symbols.
Usage:
  python manage.py refresh_all_symbols           # prices + extra for all
  python manage.py refresh_all_symbols --prices-only
  python manage.py refresh_all_symbols --extra-only
"""
from django.core.management.base import BaseCommand

from apps.financial_investments.models import Symbol
from apps.financial_investments import services
from apps.financial_investments.services_extra import refresh_extra_data


class Command(BaseCommand):
    help = 'Refresh price history and/or extra data (dividends, info, analyst, news, etc.) for all symbols.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--prices-only',
            action='store_true',
            help='Only refresh price history (fetch_and_store).',
        )
        parser.add_argument(
            '--extra-only',
            action='store_true',
            help='Only refresh extra data (dividends, company info, analyst, news, etc.).',
        )

    def handle(self, *args, **options):
        symbols = list(Symbol.objects.values_list('symbol', flat=True).order_by('symbol'))
        if not symbols:
            self.stdout.write('No symbols in database.')
            return

        do_prices = not options['extra_only']
        do_extra = not options['prices_only']
        total = len(symbols)
        self.stdout.write(f'Processing {total} symbol(s): prices={do_prices}, extra={do_extra}')

        errors = []
        for i, sym in enumerate(symbols, 1):
            self.stdout.write(f'[{i}/{total}] {sym} ...', ending='')
            try:
                if do_prices:
                    r = services.fetch_and_store(sym)
                    if r.get('error'):
                        errors.append((sym, 'prices', r['error']))
                        self.stdout.write(f' prices error: {r["error"]}')
                    else:
                        self.stdout.write(f' prices ok (created {r.get("created", 0)})', ending='')
                if do_extra:
                    r = refresh_extra_data(sym)
                    errs = r.get('errors') or {}
                    if errs:
                        errors.append((sym, 'extra', '; '.join(f'{k}: {v}' for k, v in errs.items())))
                        self.stdout.write(f' extra errors: {errs}')
                    else:
                        self.stdout.write(' extra ok')
            except Exception as e:
                errors.append((sym, 'run', str(e)))
                self.stdout.write(f' error: {e}')

        if errors:
            self.stdout.write(self.style.WARNING(f'\n{len(errors)} error(s):'))
            for sym, kind, msg in errors:
                self.stdout.write(f'  {sym} ({kind}): {msg}')
        self.stdout.write(self.style.SUCCESS(f'\nDone. Processed {total} symbol(s).'))
