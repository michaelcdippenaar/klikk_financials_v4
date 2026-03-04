"""
Parser for Xero Balance Sheet Excel exports.

Reads the first sheet (typically "Balance Sheet"), extracts report date from
"As at DD Month YYYY" and data rows: account name in one column, balance in
the first date column. Skips section headers and total rows.
"""
import re
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# Rows to skip: section headers that don't have a balance (or are subtotals)
SKIP_FIRST_CELL_STARTS = (
    'Balance Sheet', 'Klikk', 'As at', 'Account',  # Title/header
    'Assets', 'Liabilities', 'Equity', 'Bank', 'Current Assets',
    'Fixed Assets', 'Total Bank', 'Total Current', 'Total Fixed',
    'Total Assets', 'Total Liabilities', 'Total Equity',
)
SKIP_FIRST_CELL_CONTAINS = ('Total ', 'total ')


def _safe_decimal(value):
    if value is None or value == '' or (isinstance(value, float) and (value != value or value == 0)):
        return None
    try:
        if isinstance(value, (int, float)):
            return Decimal(str(value))
        s = str(value).strip().replace(',', '')
        if not s:
            return None
        return Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_report_date(cell_value):
    """Parse 'As at 3 March 2026' or similar from a cell."""
    if not cell_value:
        return None
    s = str(cell_value).strip()
    # "As at 3 March 2026" or "As at 03/03/2026"
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', s, re.I)
    if m:
        try:
            day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
            dt = datetime.strptime(f'{day} {month_name} {year}', '%d %B %Y')
            return dt.date()
        except ValueError:
            pass
    return None


def parse_balance_sheet_excel(file_path, sheet_name=0, account_col=1, value_col=2, header_row_index=4, report_date_row_index=2, report_date_col=0):
    """
    Parse a Xero Balance Sheet Excel file.

    Args:
        file_path: Path to the .xlsx file
        sheet_name: Sheet index or name (default 0 = first sheet)
        account_col: 0-based column index for account name (default 1)
        value_col: 0-based column index for balance value - first date column (default 2)
        header_row_index: Row index of header row (default 4)
        report_date_row_index: Row index containing "As at DD Month YYYY" (default 2)
        report_date_col: Column index for report date (default 0)

    Returns:
        dict with:
            - report_date: date or None if not found
            - rows: list of {"account_name": str, "account_code": str, "value": Decimal}
            - parse_errors: list of str (optional)
    """
    import pandas as pd

    df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)
    rows_out = []
    errors = []

    # Report date from row 2, col 0
    if report_date_row_index < len(df) and report_date_col is not None:
        cell = df.iloc[report_date_row_index, report_date_col]
        report_date = _parse_report_date(cell)
    else:
        report_date = None

    # Data starts after header (header_row_index + 2 to skip header and blank)
    start_row = header_row_index + 2
    for i in range(start_row, len(df)):
        account_cell = df.iloc[i, account_col]
        value_cell = df.iloc[i, value_col]

        account_name = None
        if pd.notna(account_cell) and str(account_cell).strip():
            account_name = str(account_cell).strip()

        if not account_name:
            continue

        # Skip section headers and total rows
        first_word = account_name.split()[0] if account_name else ''
        if first_word in SKIP_FIRST_CELL_STARTS or any(s in account_name for s in SKIP_FIRST_CELL_CONTAINS):
            continue

        value = _safe_decimal(value_cell)
        if value is None:
            continue

        # Optional: extract code from "Name (CODE)" at end
        account_code = ''
        m = re.search(r'\s*\(([^)]+)\)\s*$', account_name)
        if m:
            account_code = m.group(1).strip()

        rows_out.append({
            'account_name': account_name,
            'account_code': account_code,
            'value': value,
        })

    logger.info("Parsed %d balance sheet rows from Excel (report_date=%s)", len(rows_out), report_date)
    return {
        'report_date': report_date,
        'rows': rows_out,
        'parse_errors': errors,
    }
