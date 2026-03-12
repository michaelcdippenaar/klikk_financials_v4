"""
Import tickers from Share_Codes_Names_Company_yfinance_full.xlsx (or similar).
Creates/updates Investec JSE share name mapping and financial_investments Symbol,
links them, and optionally fetches yfinance price data.
"""
import pandas as pd
from django.core.management.base import BaseCommand

from apps.investec.models import InvestecJseShareNameMapping
from apps.financial_investments.models import Symbol
from apps.financial_investments import services


def str_clean(s, max_len=None):
    if s is None or (hasattr(s, '__float__') and pd.isna(s)):
        return ''
    out = str(s).strip()
    if max_len and len(out) > max_len:
        return out[:max_len]
    return out


class Command(BaseCommand):
    help = (
        "Import tickers from Excel (columns: Share_Name, Share_Name2, Share_Name3, Company, Share_Code, yfinance_Ticker). "
        "Updates Investec JSE share name mapping and financial_investments Symbol, links them, and optionally fetches data."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'excel_path',
            type=str,
            help='Path to Excel file (e.g. Share_Codes_Names_Company_yfinance_full.xlsx)',
        )
        parser.add_argument(
            '--no-fetch',
            action='store_true',
            help='Do not fetch yfinance price data after import.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only print what would be done, do not write to DB or fetch.',
        )

    def handle(self, *args, **options):
        path = options['excel_path']
        do_fetch = not options['no_fetch']
        dry_run = options['dry_run']

        try:
            df = pd.read_excel(path)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Failed to read Excel: {e}'))
            return

        required = {'Share_Code', 'yfinance_Ticker'}
        missing = required - set(df.columns)
        if missing:
            self.stdout.write(self.style.ERROR(f'Excel missing columns: {missing}. Expected at least: Share_Code, yfinance_Ticker'))
            return

        # Optional columns for mapping
        share_name_col = 'Share_Name' if 'Share_Name' in df.columns else None
        share_name2_col = 'Share_Name2' if 'Share_Name2' in df.columns else None
        share_name3_col = 'Share_Name3' if 'Share_Name3' in df.columns else None
        company_col = 'Company' if 'Company' in df.columns else None

        created_mappings = 0
        updated_mappings = 0
        created_symbols = 0
        linked = 0
        fetch_ok = 0
        fetch_err = 0

        for idx, row in df.iterrows():
            share_code = str_clean(row.get('Share_Code'), 20)
            yf_ticker = str_clean(row.get('yfinance_Ticker'), 20)
            if not yf_ticker or yf_ticker.lower() in ('nan', 'none', ''):
                continue
            yf_ticker = yf_ticker.upper()

            share_name = str_clean(row.get(share_name_col) if share_name_col else row.get('Share_Name'), 100) or share_code
            share_name2 = str_clean(row.get(share_name2_col) if share_name2_col else row.get('Share_Name2'), 100)
            share_name3 = str_clean(row.get(share_name3_col) if share_name3_col else row.get('Share_Name3'), 100)
            company = str_clean(row.get(company_col) if company_col else row.get('Company'), 100)

            if dry_run:
                self.stdout.write(f'  Would process: Share_Code={share_code!r} -> yfinance={yf_ticker!r} Company={company!r}')
                continue

            # Get or create Investec JSE share name mapping (by share_code if present, else by share_name)
            mapping = None
            if share_code:
                mapping = InvestecJseShareNameMapping.objects.filter(share_code=share_code).first()
            if not mapping and share_name:
                mapping = InvestecJseShareNameMapping.objects.filter(share_name=share_name).first()
            if not mapping:
                if share_name or share_code:
                    mapping = InvestecJseShareNameMapping.objects.create(
                        share_name=share_name or share_code,
                        share_name2=share_name2 or None,
                        share_name3=share_name3 or None,
                        company=company or None,
                        share_code=share_code or None,
                    )
                    created_mappings += 1
                    self.stdout.write(self.style.SUCCESS(f'  Created mapping: {mapping.share_name} ({mapping.share_code})'))
                else:
                    self.stdout.write(self.style.WARNING(f'  Skip (no share_code/share_name): yfinance={yf_ticker}'))
                    continue
            else:
                updated = []
                if company and mapping.company != company:
                    mapping.company = company
                    updated.append('company')
                if share_name and mapping.share_name != share_name:
                    mapping.share_name = share_name
                    updated.append('share_name')
                if share_name2 is not None and mapping.share_name2 != share_name2:
                    mapping.share_name2 = share_name2 or None
                    updated.append('share_name2')
                if share_name3 is not None and mapping.share_name3 != share_name3:
                    mapping.share_name3 = share_name3 or None
                    updated.append('share_name3')
                if share_code and mapping.share_code != share_code:
                    mapping.share_code = share_code
                    updated.append('share_code')
                if updated:
                    mapping.save(update_fields=list(set(updated)))
                    updated_mappings += 1

            # Get or create Symbol and link to mapping
            symbol_obj, symbol_created = Symbol.objects.get_or_create(
                symbol=yf_ticker,
                defaults={'name': company, 'exchange': ''},
            )
            if symbol_created:
                created_symbols += 1
            if company and symbol_obj.name != company:
                symbol_obj.name = company
                symbol_obj.save(update_fields=['name', 'updated_at'])
            if symbol_obj.share_name_mapping_id != (mapping.pk if mapping else None):
                symbol_obj.share_name_mapping = mapping
                symbol_obj.save(update_fields=['share_name_mapping', 'updated_at'])
                linked += 1

            if do_fetch:
                result = services.fetch_and_store(yf_ticker)
                if result.get('error'):
                    self.stdout.write(self.style.ERROR(f'  {yf_ticker}: fetch failed - {result["error"]}'))
                    fetch_err += 1
                else:
                    n = result.get('created', 0)
                    self.stdout.write(self.style.SUCCESS(f'  {yf_ticker}: stored {n} price points'))
                    fetch_ok += 1

        if dry_run:
            self.stdout.write(self.style.SUCCESS('Dry run complete.'))
            return

        self.stdout.write(
            self.style.SUCCESS(
                f'Import complete: mappings created={created_mappings} updated={updated_mappings}, '
                f'symbols created={created_symbols}, linked={linked}'
            )
        )
        if do_fetch:
            self.stdout.write(self.style.SUCCESS(f'Fetch: {fetch_ok} ok, {fetch_err} errors.'))
