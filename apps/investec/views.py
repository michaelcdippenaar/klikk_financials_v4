import pandas as pd
import re
import os
import io
from datetime import datetime, timedelta
import traceback
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from django.utils import timezone
from decimal import Decimal, InvalidOperation
from .models import InvestecJseTransaction, InvestecJsePortfolio, InvestecJseShareNameMapping, InvestecJseShareMonthlyPerformance, InvestecBankAccount, InvestecBankTransaction, InvestecBankSyncLog
from .serializers import InvestecJseTransactionSerializer, InvestecJsePortfolioSerializer, InvestecJseShareNameMappingSerializer
from .bank_sync import run_investec_bank_sync



# ------------------------------------------------
# Import Transaction Data
# ------------------------------------------------

def calculate_dividend_ttm(transactions_to_create):
    """
    Calculate trailing 12-month (TTM) dividend sum for each transaction.
    Calculates TTM separately for each dividend type (Dividend, Special Dividend, Foreign Dividend).
    
    Steps:
    1. Get all existing dividend transactions from database
    2. Combine with new transactions being uploaded
    3. Filter to dividend types
    4. Group by share_name AND dividend_type, then resample to monthly (month-end)
    5. Fill missing months with 0
    6. Calculate rolling 12-month sum
    7. Store TTM summary records in database for all months (even months without dividends)
    8. Return lookup dictionary: (share_name, dividend_type, year, month) -> dividend_ttm
    """
    # Dividend types to include
    dividend_types = ['Dividend', 'Special Dividend', 'Foreign Dividend', 'Dividend Tax']
    
    # Get all existing dividend transactions from database
    # Exclude TTM summary records (they have quantity=0, value=0, description starts with 'TTM Summary') as we only want actual transactions
    # Use Q object to ensure proper exclusion: exclude records where ALL three conditions are true
    existing_dividends = InvestecJseTransaction.objects.filter(
        type__in=dividend_types,
        share_name__isnull=False
    ).exclude(share_name='').exclude(
        Q(quantity=0) & Q(value=0) & Q(description__startswith='TTM Summary')
    ).values('date', 'share_name', 'type', 'value', 'account_number', 'year', 'month')
    
    # Convert to list of dicts for pandas
    existing_data = [
        {
            'date': item['date'],
            'share_name': item['share_name'],
            'dividend_type': item['type'],  # Include dividend type
            'value': float(item['value']),
            'account_number': item['account_number'],
            'year': item['year'],
            'month': item['month']
        }
        for item in existing_dividends
    ]
    
    # Add new transactions being uploaded (only dividend types with share_name)
    # Exclude TTM summary records: they have quantity=0, value=0, and description starts with 'TTM Summary'
    new_dividends = []
    for txn in transactions_to_create:
        if (txn.type in dividend_types and 
            txn.share_name and txn.share_name.strip() and
            not (txn.quantity == 0 and txn.value == 0 and txn.description.startswith('TTM Summary'))):
            new_dividends.append({
                'date': txn.date,
                'share_name': txn.share_name,
                'dividend_type': txn.type,  # Include dividend type
                'value': float(txn.value),
                'account_number': txn.account_number,
                'year': txn.year,
                'month': txn.month
            })
    
    # Combine existing and new
    all_dividends = existing_data + new_dividends
    
    if not all_dividends:
        return {}  # No dividends to process
    
    # Create DataFrame immediately after combining existing and new records
    # This is critical: we must convert to DataFrame before any aggregation to enable deduplication
    df = pd.DataFrame(all_dividends)
    
    # CRITICAL: Ensure we exclude any TTM summary records that might have slipped through
    # TTM summary records have value=0, but we also check account_number if available
    # Filter out any records where value is 0 (TTM summary records have value=0)
    # However, we need to be careful: actual dividend transactions should never have value=0
    # So filtering by value != 0 should be safe
    if 'value' in df.columns:
        df = df[df['value'] != 0]
    
    # CRITICAL: Remove duplicates immediately after DataFrame creation, BEFORE any aggregation/summing operations.
    # This prevents double-counting when uploading files that overlap with existing database records.
    # If a transaction exists in both the database and the upload file, we keep only the first occurrence.
    # Deduplication is based on: share_name, dividend_type, date, and value (to identify unique transactions).
    df = df.drop_duplicates(subset=['share_name', 'dividend_type', 'date', 'value'], keep='first')
    
    # Convert date to datetime if needed
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
    
    # Group by share_name AND dividend_type, then process each group
    ttm_lookup = {}
    # Store TTM values per share_name AND dividend_type (separate records per dividend type)
    share_ttm_data = {}  # {(share_name, dividend_type, year, month): {'ttm': ttm_value, 'account_number': account_number}}
    
    for (share_name, dividend_type), group_df in df.groupby(['share_name', 'dividend_type']):
        # Get the account_number from the first transaction in this group
        # All transactions for the same share/dividend_type should have the same account_number
        account_number = group_df['account_number'].iloc[0] if 'account_number' in group_df.columns and len(group_df) > 0 else ''
        
        # Sort by date
        group_df = group_df.sort_values('date')
        
        # Set date as index for resampling
        group_df_indexed = group_df.set_index('date')
        
        # Resample to monthly (month-end) and sum dividends per month
        monthly_df = group_df_indexed.resample('ME').agg({
            'value': 'sum'
        })
        
        # Fill missing months with 0
        # Create complete date range from earliest to latest date
        if len(monthly_df) > 0:
            min_date = monthly_df.index.min()
            max_date = monthly_df.index.max()
            
            # CRITICAL: Extend max_date to at least the current month-end.
            # This ensures the rolling TTM calculation runs all the way to the present day.
            # Without this extension, reports for the current month might show empty/incorrect TTM values
            # if there hasn't been a recent dividend transaction in the current month.
            # The rolling window needs complete monthly data up to today to calculate accurate TTM values.
            current_month_end = pd.Timestamp.now().to_period('M').to_timestamp('M')
            if max_date < current_month_end:
                max_date = current_month_end
            
            date_range = pd.date_range(start=min_date, end=max_date, freq='ME')
            
            # Reindex to fill missing months with 0
            monthly_df = monthly_df.reindex(date_range, fill_value=0)
            
            # Ensure value column exists and is numeric
            if 'value' not in monthly_df.columns:
                monthly_df['value'] = 0
            monthly_df['value'] = pd.to_numeric(monthly_df['value'], errors='coerce').fillna(0)
            
            # Calculate rolling 12-month sum (window includes current month)
            monthly_df['dividend_ttm'] = monthly_df['value'].rolling(window=12, min_periods=1).sum()
            
            # Create lookup dictionary: (share_name, dividend_type, year, month) -> dividend_ttm
            for idx, row in monthly_df.iterrows():
                # Get year and month from the index (month-end date)
                year = idx.year
                month = idx.month
                
                # Get TTM value and round to 2 decimal places for clean database storage
                # Rounding prevents floating-point precision issues and ensures consistent values
                ttm_value = Decimal(str(round(row['dividend_ttm'], 2)))
                
                # Store in lookup with dividend_type included (for transaction-level TTM)
                ttm_lookup[(share_name, dividend_type, year, month)] = ttm_value
                
                # Store TTM per dividend_type (separate records per dividend type)
                key = (share_name, dividend_type, year, month)
                share_ttm_data[key] = {
                    'ttm': ttm_value,
                    'account_number': account_number
                }
    
    # Now create InvestecJseShareMonthlyPerformance records
    # Get all unique share_names and date ranges
    if share_ttm_data:
        # Get portfolio data to find closing prices
        # Map share_name to share_code using InvestecJseShareNameMapping
        share_name_to_code = {}
        mappings = InvestecJseShareNameMapping.objects.filter(
            share_name__in=[key[0] for key in share_ttm_data.keys()]
        ).values('share_name', 'share_code')
        for mapping in mappings:
            if mapping['share_code']:
                share_name_to_code[mapping['share_name']] = mapping['share_code']
        
        # Get portfolio data (quantity, price, total_value) for all relevant dates
        # Keys are (share_name, dividend_type, year, month), so we need key[2] and key[3] for year and month
        all_dates = set((key[2], key[3]) for key in share_ttm_data.keys())  # (year, month) tuples from (share_name, dividend_type, year, month)
        portfolio_data = {}  # {(share_code, year, month): {'quantity': qty, 'price': price, 'total_value': total_value}}
        
        # Get current year and month for comparison
        current_date = datetime.now()
        current_year = current_date.year
        current_month = current_date.month
        
        for (year, month) in all_dates:
            # Check if this is the current month
            is_current_month = (year == current_year and month == current_month)
            
            if is_current_month:
                # For current month: Get the latest portfolio data within that month
                # Get all portfolio records for this month, then get the latest one per share_code
                all_portfolios = InvestecJsePortfolio.objects.filter(
                    year=year,
                    month=month
                ).values('share_code', 'quantity', 'price', 'total_value', 'date').order_by('share_code', '-date')
                
                # Group by share_code and take the first (latest) record for each
                latest_by_share = {}
                for portfolio in all_portfolios:
                    share_code = portfolio['share_code']
                    if share_code not in latest_by_share:
                        latest_by_share[share_code] = portfolio
                
                # Store the latest data for each share_code
                for share_code, portfolio in latest_by_share.items():
                    portfolio_data[(share_code, year, month)] = {
                        'quantity': Decimal(str(portfolio['quantity'])),
                        'price': Decimal(str(portfolio['price'])),
                        'total_value': Decimal(str(portfolio['total_value']))
                    }
            else:
                # For historical months: Get portfolio data for month-end date
                # NOTE: pandas Timestamp is picky: if `year` is a string it treats it as a date string input
                # and then passing additional date parts triggers:
                # "Cannot pass a date attribute keyword argument when passing a date string; 'tz' is keyword-only"
                # So we coerce year/month to int and use keyword args.
                month_end = pd.Timestamp(year=int(year), month=int(month), day=1).to_period('M').to_timestamp('M').date()
                
                portfolios = InvestecJsePortfolio.objects.filter(
                    date=month_end
                ).values('share_code', 'quantity', 'price', 'total_value')
                
                for portfolio in portfolios:
                    portfolio_data[(portfolio['share_code'], year, month)] = {
                        'quantity': Decimal(str(portfolio['quantity'])),
                        'price': Decimal(str(portfolio['price'])),
                        'total_value': Decimal(str(portfolio['total_value']))
                    }
        
        # Create InvestecJseShareMonthlyPerformance records
        performance_records = []
        
        for (share_name, dividend_type, year, month), data in share_ttm_data.items():
            ttm_value = data['ttm']
            account_number = data['account_number']
            # Coerce year/month to int for the same reason as above (pandas Timestamp parsing).
            month_end_date = pd.Timestamp(year=int(year), month=int(month), day=1).to_period('M').to_timestamp('M').date()
            
            # Get portfolio data (quantity, price, total_value) from portfolio if available
            closing_price = None
            quantity = None
            total_market_value = None
            share_code = share_name_to_code.get(share_name)
            if share_code:
                portfolio_info = portfolio_data.get((share_code, year, month))
                if portfolio_info:
                    closing_price = portfolio_info['price']
                    quantity = portfolio_info['quantity']
                    total_market_value = portfolio_info['total_value']
            
            # Calculate dividend yield: Dividend Yield = Total Dividend Cash Received TTM / Total Market Value
            # Total Market Value = Quantity × Price (from portfolio)
            # If no portfolio data exists for a month, set dividend_yield = 0
            dividend_yield = Decimal('0')  # Default to 0 if no portfolio data
            if total_market_value and total_market_value > 0 and ttm_value > 0:
                dividend_yield = (ttm_value / total_market_value)
            
            performance_records.append(
                InvestecJseShareMonthlyPerformance(
                    share_name=share_name,
                    date=month_end_date,
                    year=year,
                    month=month,
                    dividend_type=dividend_type,
                    investec_account=account_number,
                    dividend_ttm=ttm_value,
                    closing_price=closing_price,
                    quantity=quantity,
                    total_market_value=total_market_value,
                    dividend_yield=dividend_yield,
                )
            )
        
        # Store InvestecJseShareMonthlyPerformance records using bulk operations
        # Delete existing records for these shares/dates/dividend_types, then bulk create new ones
        if performance_records:
            # Get unique share_names, dividend_types, and date range
            share_names = list(set(rec.share_name for rec in performance_records))
            dividend_types = list(set(rec.dividend_type for rec in performance_records))
            min_date = min(rec.date for rec in performance_records)
            max_date = max(rec.date for rec in performance_records)
            
            # Delete existing records for this date range and dividend types
            InvestecJseShareMonthlyPerformance.objects.filter(
                share_name__in=share_names,
                dividend_type__in=dividend_types,
                date__gte=min_date,
                date__lte=max_date
            ).delete()
            
            # Bulk create new records
            InvestecJseShareMonthlyPerformance.objects.bulk_create(performance_records, ignore_conflicts=False)
    
    return ttm_lookup


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def excel_upload_view(request):
    """
    API endpoint to upload Excel file and import transactions.
    
    Accepts POST request with 'file' field containing Excel file.
    Returns import statistics and any errors encountered.
    """
    if 'file' not in request.FILES:
        return Response(
            {'error': 'No file provided. Please upload an Excel file.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    uploaded_file = request.FILES['file']
    
    # Validate file extension
    if not uploaded_file.name.endswith(('.xlsx', '.xls')):
        return Response(
            {'error': 'Invalid file format. Please upload an Excel file (.xlsx or .xls).'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        # Read Excel file - try to find header row
        # First, read without header to inspect structure
        df_raw = pd.read_excel(uploaded_file, header=None)
        
        # Find header row (look for row containing 'Date' and 'Account Number')
        header_row = None
        for idx, row in df_raw.iterrows():
            row_str = ' '.join([str(val).lower() for val in row.values if pd.notna(val)])
            if 'date' in row_str and 'account' in row_str:
                header_row = idx
                break
        
        # If header row found, read with that row as header
        if header_row is not None:
            # Read with header_row as column names - pandas automatically starts data from next row
            df = pd.read_excel(uploaded_file, header=header_row)
        else:
            # Fallback: read normally and try to detect columns
            df = pd.read_excel(uploaded_file)
        
        # Normalize column names (remove spaces, convert to lowercase)
        df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_').str.replace('-', '_')
        
        # Map common column name variations to model fields
        column_mapping = {
            'date': ['date', 'transaction_date', 'trade_date'],
            'account_number': ['account_number', 'account', 'account_no', 'accountnum'],
            'description': ['description', 'desc', 'details'],
            'share_name': ['share_name', 'sharename', 'stock_name', 'stock', 'instrument', 'security', 'share_name'],
            'type': ['type', 'action', 'transaction_type', 'transaction', 'side'],
            'quantity': ['quantity', 'qty', 'shares', 'units'],
            'value': ['value', 'amount', 'price', 'total', 'transaction_value'],
        }
        
        # Find actual column names in the dataframe
        actual_columns = {}
        for model_field, possible_names in column_mapping.items():
            for possible_name in possible_names:
                if possible_name in df.columns:
                    actual_columns[model_field] = possible_name
                    break
        
        # Check if all required columns are found (type can be extracted from description)
        required_fields = ['date', 'account_number', 'description', 'share_name', 'quantity', 'value']
        missing_fields = [field for field in required_fields if field not in actual_columns]
        
        if missing_fields:
            return Response(
                {
                    'error': f'Missing required columns: {", ".join(missing_fields)}',
                    'available_columns': list(df.columns),
                    'suggestion': 'Please ensure your Excel file contains columns matching: date, account_number, description, share_name, quantity, value'
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Extract date range from filename (format: TransactionHistory-All-YYYYMMDD-YYYYMMDD.xlsx)
        from_date = None
        to_date = None
        
        filename = uploaded_file.name
        # Try to extract dates from filename pattern: ...-YYYYMMDD-YYYYMMDD...
        date_pattern = re.search(r'(\d{8})-(\d{8})', filename)
        if date_pattern:
            try:
                from_date_str = date_pattern.group(1)
                to_date_str = date_pattern.group(2)
                from_date = pd.to_datetime(from_date_str, format='%Y%m%d').date()
                to_date = pd.to_datetime(to_date_str, format='%Y%m%d').date()
            except:
                pass
        
        # If not found in filename, try to find dates in the Excel file itself
        if from_date is None or to_date is None:
            # Look for "From" and "To" date patterns in the raw data
            for idx, row in df_raw.iterrows():
                row_str = ' '.join([str(val) for val in row.values if pd.notna(val)])
                row_lower = row_str.lower()
                
                # Look for "from date" or "to date" patterns
                if 'from' in row_lower and 'date' in row_lower:
                    # Try to extract date from this row
                    for cell_val in row.values:
                        if pd.notna(cell_val):
                            cell_str = str(cell_val).strip()
                            # Try various date formats
                            for date_format in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y%m%d', '%d %B %Y', '%d %b %Y']:
                                try:
                                    parsed_date = pd.to_datetime(cell_str, format=date_format).date()
                                    if from_date is None:
                                        from_date = parsed_date
                                    elif to_date is None and parsed_date > from_date:
                                        to_date = parsed_date
                                    break
                                except:
                                    try:
                                        parsed_date = pd.to_datetime(cell_str).date()
                                        if from_date is None:
                                            from_date = parsed_date
                                        elif to_date is None and parsed_date > from_date:
                                            to_date = parsed_date
                                        break
                                    except:
                                        continue
                
                if 'to' in row_lower and 'date' in row_lower:
                    # Try to extract date from this row
                    for cell_val in row.values:
                        if pd.notna(cell_val):
                            cell_str = str(cell_val).strip()
                            # Try various date formats
                            for date_format in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y%m%d', '%d %B %Y', '%d %b %Y']:
                                try:
                                    parsed_date = pd.to_datetime(cell_str, format=date_format).date()
                                    if to_date is None or parsed_date > to_date:
                                        to_date = parsed_date
                                    break
                                except:
                                    try:
                                        parsed_date = pd.to_datetime(cell_str).date()
                                        if to_date is None or parsed_date > to_date:
                                            to_date = parsed_date
                                        break
                                    except:
                                        continue
                
                # Stop searching after finding both dates or after checking first 20 rows
                if from_date and to_date or idx > 20:
                    break
        
        # Clear existing transactions only for the date range being uploaded
        if from_date and to_date:
            deleted_count = InvestecJseTransaction.objects.filter(
                date__gte=from_date,
                date__lte=to_date
            ).delete()[0]
        else:
            # If we can't determine the date range, don't delete anything
            # This is safer than deleting all transactions
            deleted_count = 0
        
        # Prepare data for bulk creation
        transactions_to_create = []
        errors = []
        
        for index, row in df.iterrows():
            try:
                # Parse date
                date_value = row[actual_columns['date']]
                
                # Skip rows with empty date (header rows that might have been included)
                if pd.isna(date_value) or (isinstance(date_value, str) and str(date_value).strip() == ''):
                    continue
                
                # Handle different date formats
                if isinstance(date_value, str):
                    parsed_date = parse_date(date_value)
                    if not parsed_date:
                        # Try pandas to_datetime
                        try:
                            parsed_date = pd.to_datetime(date_value).date()
                        except:
                            errors.append(f'Row {index + 2}: Invalid date format: {date_value}')
                            continue
                elif isinstance(date_value, pd.Timestamp):
                    parsed_date = date_value.date()
                elif hasattr(date_value, 'date'):  # datetime object
                    parsed_date = date_value.date()
                else:
                    # Try to convert to date
                    try:
                        parsed_date = pd.to_datetime(date_value).date()
                    except:
                        errors.append(f'Row {index + 2}: Invalid date format: {date_value}')
                        continue
                
                # Parse quantity
                quantity_value = row[actual_columns['quantity']]
                if pd.isna(quantity_value):
                    errors.append(f'Row {index + 2}: Quantity is missing')
                    continue
                try:
                    quantity = Decimal(str(quantity_value))
                except (InvalidOperation, ValueError):
                    errors.append(f'Row {index + 2}: Invalid quantity value: {quantity_value}')
                    continue
                
                # Get description early to check for dividend patterns
                description_val = row[actual_columns['description']]
                description = str(description_val)[:255] if not pd.isna(description_val) else ''
                
                # For dividends, quantity might be in description
                # Patterns: "DIV. 327 NINETY 1L" -> quantity is 327
                #          "FOREIGN DIV. 3061 BATS" -> quantity is 3061
                #          "DIV. TAX ON 74 NINETY 1L" -> quantity is 74
                #          "SPEC.DIV. 1229 OUTSURE" -> quantity is 1229
                if quantity == 0 and description:
                    description_upper = description.upper()
                    if 'FOREIGN DIV' in description_upper:
                        # Extract quantity from pattern like "FOREIGN DIV. 3061 BATS"
                        foreign_div_match = re.search(r'FOREIGN\s+DIV\.?\s*(\d+)', description, re.IGNORECASE)
                        if foreign_div_match:
                            try:
                                quantity = Decimal(foreign_div_match.group(1))
                            except (InvalidOperation, ValueError):
                                pass  # Keep original quantity if extraction fails
                    elif 'SPEC.DIV' in description_upper or 'SPECIAL DIV' in description_upper or 'SPECIAL DIVIDEND' in description_upper:
                        # Extract quantity from pattern like "SPEC.DIV. 1229 OUTSURE"
                        spec_div_match = re.search(r'SPEC(?:IAL)?\.?\s*DIV(?:IDEND)?\.?\s*(\d+)', description, re.IGNORECASE)
                        if spec_div_match:
                            try:
                                quantity = Decimal(spec_div_match.group(1))
                            except (InvalidOperation, ValueError):
                                pass  # Keep original quantity if extraction fails
                    elif 'DIV. TAX' in description_upper or 'DIVIDEND TAX' in description_upper:
                        # Extract quantity from pattern like "DIV. TAX ON 74 NINETY 1L"
                        div_tax_match = re.search(r'DIV\.?\s*TAX\s+ON\s+(\d+)', description, re.IGNORECASE)
                        if div_tax_match:
                            try:
                                quantity = Decimal(div_tax_match.group(1))
                            except (InvalidOperation, ValueError):
                                pass  # Keep original quantity if extraction fails
                    elif description_upper.startswith('DIV'):
                        # Extract quantity from pattern like "DIV. 327 NINETY 1L"
                        div_match = re.search(r'DIV\.?\s*(\d+)', description, re.IGNORECASE)
                        if div_match:
                            try:
                                quantity = Decimal(div_match.group(1))
                            except (InvalidOperation, ValueError):
                                pass  # Keep original quantity if extraction fails
                
                # Parse value
                value_value = row[actual_columns['value']]
                if pd.isna(value_value):
                    errors.append(f'Row {index + 2}: Value is missing')
                    continue
                try:
                    value = Decimal(str(value_value))
                except (InvalidOperation, ValueError):
                    errors.append(f'Row {index + 2}: Invalid value: {value_value}')
                    continue
                
                # Get other fields
                # Account number - handle both string and numeric values
                account_number_val = row[actual_columns['account_number']]
                if pd.isna(account_number_val):
                    account_number = ''
                else:
                    # Convert to string, handling numeric values
                    account_number = str(int(account_number_val)) if isinstance(account_number_val, (int, float)) else str(account_number_val)
                    account_number = account_number[:50]
                
                # Share name - try to extract from description if missing
                share_name_val = row[actual_columns['share_name']]
                if pd.isna(share_name_val) or str(share_name_val).strip() == '':
                    # Try to extract share name from description
                    if description:
                        description_upper = description.upper()
                        # For foreign dividends: "FOREIGN DIV. 3061 BATS" -> extract "BATS"
                        #                      "FOREIGN DIV. 123 A V I" -> extract "A V I" and convert to "AVI"
                        if 'FOREIGN DIV' in description_upper:
                            # Try to match spaced letters first (e.g., "A V I" -> "AVI")
                            foreign_spaced_match = re.search(r'FOREIGN\s+DIV\.?\s*\d+\s+((?:[A-Z]\s+)+[A-Z])', description, re.IGNORECASE)
                            if foreign_spaced_match:
                                # Remove spaces from spaced letters (e.g., "A V I" -> "AVI")
                                spaced_name = foreign_spaced_match.group(1)
                                share_name = spaced_name.replace(' ', '').upper()[:100]
                            else:
                                # Try regular word pattern
                                foreign_div_share_match = re.search(r'FOREIGN\s+DIV\.?\s*\d+\s+(\w+)', description, re.IGNORECASE)
                                if foreign_div_share_match:
                                    share_name = foreign_div_share_match.group(1).upper()[:100]
                                else:
                                    # Fallback: look for uppercase words after the number
                                    words = description.split()
                                    found_number = False
                                    for word in words:
                                        if word.isdigit():
                                            found_number = True
                                        elif found_number and word.isupper() and len(word) > 2:
                                            share_name = word[:100]
                                            break
                                    else:
                                        share_name = ''
                        # For special dividends: "SPEC.DIV. 1229 OUTSURE" -> extract "OUTSURE"
                        elif 'SPEC.DIV' in description_upper or 'SPECIAL DIV' in description_upper or 'SPECIAL DIVIDEND' in description_upper:
                            # Extract share name from pattern like "SPEC.DIV. 1229 OUTSURE"
                            spec_div_share_match = re.search(r'SPEC(?:IAL)?\.?\s*DIV(?:IDEND)?\.?\s*\d+\s+(\w+)', description, re.IGNORECASE)
                            if spec_div_share_match:
                                share_name = spec_div_share_match.group(1).upper()[:100]
                            else:
                                # Fallback: look for uppercase words after the number
                                words = description.split()
                                found_number = False
                                for word in words:
                                    if word.isdigit():
                                        found_number = True
                                    elif found_number and word.isupper() and len(word) > 2:
                                        share_name = word[:100]
                                        break
                                else:
                                    share_name = ''
                        # For regular dividends: "DIV. 327 NINETY 1L" -> extract "NINETY"
                        #                    "DIV. 446 A V I" -> extract "A V I" and convert to "AVI"
                        #                    "DIV. TAX ON 74 NINETY 1L" -> extract "NINETY"
                        elif description_upper.startswith('DIV'):
                            # Handle "DIV. TAX ON" pattern: "DIV. TAX ON 74 NINETY 1L" -> "NINETY"
                            div_tax_match = re.search(r'DIV\.?\s*TAX\s+ON\s+\d+\s+(\w+)', description, re.IGNORECASE)
                            if div_tax_match:
                                share_name = div_tax_match.group(1).upper()[:100]
                            else:
                                # Try to match spaced letters first (e.g., "A V I" -> "AVI")
                                spaced_letters_match = re.search(r'DIV\.?\s*\d+\s+((?:[A-Z]\s+)+[A-Z])', description, re.IGNORECASE)
                                if spaced_letters_match:
                                    # Remove spaces from spaced letters (e.g., "A V I" -> "AVI")
                                    spaced_name = spaced_letters_match.group(1)
                                    share_name = spaced_name.replace(' ', '').upper()[:100]
                                else:
                                    # Try regular word pattern (e.g., "DIV. 327 NINETY 1L" -> "NINETY")
                                    div_share_match = re.search(r'DIV\.?\s*\d+\s+(\w+)', description, re.IGNORECASE)
                                    if div_share_match:
                                        share_name = div_share_match.group(1).upper()[:100]
                                    else:
                                        # Fallback: look for uppercase words
                                        words = description.split()
                                        for word in words:
                                            if word.isupper() and len(word) > 2 and word not in ['DIV', 'DIVIDEND', 'FOREIGN', 'TAX', 'ON']:
                                                share_name = word[:100]
                                                break
                                        else:
                                            share_name = ''
                        else:
                            # For other transactions: "Buy 179 NEDBANK" -> "NEDBANK"
                            words = description.split()
                            for word in reversed(words):  # Check from end, as share name is usually at the end
                                if word.isupper() and len(word) > 2:
                                    share_name = word[:100]
                                    break
                            else:
                                share_name = ''  # Couldn't extract, use empty
                    else:
                        share_name = ''
                else:
                    share_name = str(share_name_val)[:100]
                
                # Extract type from description if not a separate column
                if 'type' in actual_columns:
                    transaction_type = str(row[actual_columns['type']])[:50] if not pd.isna(row[actual_columns['type']]) else ''
                else:
                    # Try to extract type from description (e.g., "Buy 179 NEDBANK" -> "Buy")
                    transaction_type = ''
                    if description:
                        description_upper = description.upper()
                        # Account-related transactions (no share code)
                        if 'FEE' in description_upper or 'QUARTERLY ADMIN FEE' in description_upper:
                            transaction_type = 'Fee'
                        elif 'BROKER' in description_upper:
                            transaction_type = 'Broker Fee'
                        elif 'VAT' in description_upper:
                            transaction_type = 'VAT'
                        elif 'CAP.REDUC' in description_upper or 'CAPITAL REDUCTION' in description_upper:
                            transaction_type = 'Capital Reduction'
                        elif 'INTER A/C TRF' in description_upper or 'INTER ACCOUNT TRANSFER' in description_upper:
                            transaction_type = 'Inter Account Transfer'
                        elif 'TRF' in description_upper and ('TO' in description_upper or 'FROM' in description_upper):
                            # Handle "TRF FROM TRADING TO INCOME", "TRF INCOME TO TRADING", and similar transfer patterns
                            transaction_type = 'Transfer'
                        elif 'TRANSFER FROM' in description_upper or 'TRANSFER TO' in description_upper:
                            # Handle "TRANSFER FROM" and "TRANSFER TO" patterns
                            transaction_type = 'Transfer'
                        elif 'INVESTEC BANK' in description_upper or 'BANK TRANSFER' in description_upper:
                            transaction_type = 'Bank Transfer'
                        elif 'INTEREST' in description_upper:
                            transaction_type = 'Interest'
                        # Check for account number pattern: "10011910139 - MC DIPPENAAR" -> Transfer
                        elif re.match(r'^\d+\s*-\s*[A-Z\s]+$', description, re.IGNORECASE):
                            transaction_type = 'Transfer'
                        # Share-related transactions
                        elif 'FOREIGN DIV' in description_upper:
                            transaction_type = 'Foreign Dividend'
                        elif 'DIV. TAX' in description_upper or 'DIVIDEND TAX' in description_upper:
                            transaction_type = 'Dividend Tax'
                        elif 'SPEC.DIV' in description_upper or 'SPECIAL DIV' in description_upper or 'SPECIAL DIVIDEND' in description_upper:
                            transaction_type = 'Special Dividend'
                        elif description_upper.startswith('BUY'):
                            transaction_type = 'Buy'
                        elif description_upper.startswith('SELL'):
                            transaction_type = 'Sell'
                        elif 'DIV' in description_upper or 'DIVIDEND' in description_upper:
                            transaction_type = 'Dividend'
                        else:
                            # Default: take first word as type
                            transaction_type = description.split()[0][:50] if description.split() else ''
                
                # Validate required fields - account_number is always required
                if not account_number:
                    errors.append(f'Row {index + 2}: Missing required field (account_number)')
                    continue
                
                # Check if this is an account-related transaction (no share code)
                # These include: FEE, BROKER, VAT, CAP.REDUC, INTEREST, Bank Transfer, QUARTERLY ADMIN FEE, Transfers
                is_account_transaction = False
                if description:
                    desc_upper = description.upper()
                    account_keywords = ['FEE', 'BROKER', 'VAT', 'CAP.REDUC', 'CAPITAL REDUCTION', 
                                       'BANK TRANSFER', 'TRANSFER', 'QUARTERLY ADMIN FEE', 
                                       'INTER A/C TRF', 'INTER ACCOUNT TRANSFER', 'INVESTEC BANK',
                                       'TRF FROM', 'TRF TO', 'TRANSFER FROM', 'TRANSFER TO']
                    is_account_transaction = any(keyword in desc_upper for keyword in account_keywords)
                    
                    # Check for "TRF [something] TO [something]" pattern (e.g., "TRF INCOME TO TRADING")
                    if 'TRF' in desc_upper and 'TO' in desc_upper:
                        is_account_transaction = True
                    # Check for "TRF [something] FROM [something]" pattern
                    if 'TRF' in desc_upper and 'FROM' in desc_upper:
                        is_account_transaction = True
                    
                    # Check for account number pattern: "10011910139 - MC DIPPENAAR"
                    if re.match(r'^\d+\s*-\s*[A-Z\s]+$', description, re.IGNORECASE):
                        is_account_transaction = True
                
                # Ensure account-related types always have blank share_name
                account_types = ['VAT', 'Fee', 'Interest', 'Broker Fee', 'Capital Reduction', 
                                   'Bank Transfer', 'Inter Account Transfer', 'Transfer']
                if transaction_type in account_types:
                    share_name = ''  # Force blank share_name for account-related types
                    is_account_transaction = True
                
                # Special case: Transfer patterns should have no share name
                # Patterns: "TRF FROM TRADING TO INCOME", "TRF INCOME TO TRADING", "TRF TRADING TO INCOME", etc.
                if description:
                    desc_upper_transfer = description.upper()
                    if ('TRF' in desc_upper_transfer and ('TO' in desc_upper_transfer or 'FROM' in desc_upper_transfer)):
                        share_name = ''
                        is_account_transaction = True
                
                # For account-related transactions, allow empty share_name
                # For share transactions, use empty string if share_name is missing (model allows blank)
                if not share_name and not is_account_transaction:
                    share_name = ''  # Empty string for share transactions without share name (model allows blank)
                # If it's an account transaction, share_name remains empty
                
                # Extract value per share from description for Buy/Sell transactions
                value_per_share = None
                value_calculated = None
                if transaction_type in ['Buy', 'Sell'] and description:
                    # Pattern: "at 1,192 Cents" or "at 5000 Cents"
                    price_match = re.search(r'at\s+([\d,]+)\s+Cents', description, re.IGNORECASE)
                    if price_match:
                        price_str = price_match.group(1).replace(',', '')
                        try:
                            # Convert from cents to rands (divide by 100)
                            price_cents = Decimal(price_str)
                            value_per_share = price_cents / Decimal('100')
                            
                            # Calculate value_calculated = value_per_share * quantity
                            value_calculated = value_per_share * quantity
                            
                            # Make negative for Buy transactions
                            if transaction_type == 'Buy':
                                value_calculated = value_calculated * Decimal('-1')
                        except (InvalidOperation, ValueError):
                            pass
                
                transactions_to_create.append(
                    InvestecJseTransaction(
                        date=parsed_date,
                        year=parsed_date.year,
                        month=parsed_date.month,
                        day=parsed_date.day,
                        account_number=account_number,
                        description=description,
                        share_name=share_name,
                        type=transaction_type,
                        quantity=quantity,
                        value=value,
                        value_per_share=value_per_share,
                        value_calculated=value_calculated,
                    )
                )
            except Exception as e:
                errors.append(f'Row {index + 2}: {str(e)}')
                continue
        
        # Calculate Dividend TTM for all transactions
        try:
            ttm_lookup = calculate_dividend_ttm(transactions_to_create)
        except Exception as e:
            payload = {
                'error': f'Error processing file: {str(e)}',
                'exception_type': type(e).__name__,
                'context': 'calculate_dividend_ttm(transactions_to_create)',
                'pandas_version': getattr(pd, '__version__', None),
            }
            if getattr(settings, 'DEBUG', False):
                payload['traceback'] = traceback.format_exc()
            return Response(payload, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Dividend types that should have TTM calculated
        dividend_types = ['Dividend', 'Special Dividend', 'Foreign Dividend', 'Dividend Tax']
        
        # Map TTM values to transactions
        for txn in transactions_to_create:
            # Only calculate TTM for dividend types with share_name
            if (txn.type in dividend_types and 
                txn.share_name and txn.share_name.strip() and 
                txn.year and txn.month):
                # Lookup key now includes dividend_type: (share_name, dividend_type, year, month)
                lookup_key = (txn.share_name, txn.type, txn.year, txn.month)
                if lookup_key in ttm_lookup:
                    txn.dividend_ttm = ttm_lookup[lookup_key]
                else:
                    txn.dividend_ttm = None
            else:
                txn.dividend_ttm = None
        
        # Bulk create transactions
        created_count = 0
        if transactions_to_create:
            with transaction.atomic():
                created_instances = InvestecJseTransaction.objects.bulk_create(
                    transactions_to_create,
                    ignore_conflicts=False
                )
                created_count = len(created_instances)
        
        # Prepare response
        response_data = {
            'success': True,
            'message': f'Successfully imported {created_count} transactions',
            'deleted_previous': deleted_count,
            'total_rows': len(df),
            'created': created_count,
            'errors': len(errors),
        }
        
        # Add date range information if available
        if from_date and to_date:
            response_data['date_range'] = {
                'from_date': str(from_date),
                'to_date': str(to_date),
            }
            response_data['message'] += f' for date range {from_date} to {to_date}'
        elif from_date:
            response_data['date_range'] = {
                'from_date': str(from_date),
                'to_date': None,
            }
        elif to_date:
            response_data['date_range'] = {
                'from_date': None,
                'to_date': str(to_date),
            }
        
        if errors:
            response_data['error_details'] = errors[:50]  # Limit to first 50 errors
            if len(errors) > 50:
                response_data['error_details'].append(f'... and {len(errors) - 50} more errors')
        
        return Response(response_data, status=status.HTTP_201_CREATED if created_count > 0 else status.HTTP_200_OK)
        
    except pd.errors.EmptyDataError:
        return Response(
            {'error': 'The Excel file is empty.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        payload = {
            'error': f'Error processing file: {str(e)}',
            'exception_type': type(e).__name__,
            'pandas_version': getattr(pd, '__version__', None),
        }
        if getattr(settings, 'DEBUG', False):
            payload['traceback'] = traceback.format_exc()
        return Response(payload, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
def transaction_list_view(request):
    """
    API endpoint to list all Investec transactions.
    
    Supports query parameters:
    - limit: Number of records to return (default: 100)
    - offset: Number of records to skip (default: 0)
    - account_number: Filter by account number
    - share_name: Filter by share name
    - type: Filter by type (Buy, Sell, Dividend, etc.)
    - include_ttm_summary: Include TTM summary records (default: True). Set to 'false' to exclude TTM summary records.
    """
    queryset = InvestecJseTransaction.objects.all()
    
    # Filter out TTM summary records by default, unless explicitly requested
    # TTM summary records are identified by quantity=0, value=0, and description starting with 'TTM Summary'
    include_ttm_summary = request.query_params.get('include_ttm_summary', 'false').lower() == 'true'
    if not include_ttm_summary:
        queryset = queryset.exclude(
            quantity=0,
            value=0,
            description__startswith='TTM Summary'
        )
    
    # Apply filters
    account_number = request.query_params.get('account_number', None)
    if account_number:
        queryset = queryset.filter(account_number=account_number)
    
    share_name = request.query_params.get('share_name', None)
    if share_name:
        queryset = queryset.filter(share_name__icontains=share_name)
    
    transaction_type = request.query_params.get('type', None)
    if transaction_type:
        queryset = queryset.filter(type__icontains=transaction_type)
    
    # Apply pagination
    limit = int(request.query_params.get('limit', 100))
    offset = int(request.query_params.get('offset', 0))
    
    total_count = queryset.count()
    transactions = queryset[offset:offset + limit]
    
    serializer = InvestecJseTransactionSerializer(transactions, many=True)
    
    return Response({
        'count': total_count,
        'limit': limit,
        'offset': offset,
        'results': serializer.data
    })

# ------------------------------------------------
# Import Portfolio Data
# ------------------------------------------------

def process_portfolio_file(uploaded_file):
    """
    Helper function to process a single portfolio Excel file.
    Returns a dict with results or error information.
    """
    # Validate file extension
    if not uploaded_file.name.endswith(('.xlsx', '.xls')):
        return {
            'success': False,
            'filename': uploaded_file.name,
            'error': 'Invalid file format. Please upload an Excel file (.xlsx or .xls).'
        }
    
    try:
        # Read Excel file without header to inspect structure
        df_raw = pd.read_excel(uploaded_file, header=None)
        
        # First, find the row containing "Portfolio Holdings Report"
        report_row = None
        for idx, row in df_raw.iterrows():
            row_str = ' '.join([str(val) for val in row.values if pd.notna(val)])
            if 'portfolio holdings report' in row_str.lower():
                report_row = idx
                break
        
        if report_row is None:
            return {
                'success': False,
                'filename': uploaded_file.name,
                'error': 'Could not find "Portfolio Holdings Report" header in Excel file.'
            }
        
        # Extract date from Excel file - look for date patterns in rows around the report header
        portfolio_date = None
        
        # Try to extract date from filename first (format: Holdings-YYYYMMDD...)
        filename = uploaded_file.name
        date_match = re.search(r'(\d{8})', filename)
        if date_match:
            try:
                date_str = date_match.group(1)
                portfolio_date = pd.to_datetime(date_str, format='%Y%m%d').date()
            except:
                pass
        
        # If not found in filename, look for date in Excel rows around the report header
        if portfolio_date is None:
            # Check rows before and after the report header for date patterns
            search_rows = list(range(max(0, report_row - 5), min(len(df_raw), report_row + 5)))
            for idx in search_rows:
                row = df_raw.iloc[idx]
                for cell_val in row.values:
                    if pd.notna(cell_val):
                        cell_str = str(cell_val).strip()
                        # Try various date formats
                        for date_format in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y%m%d', '%d %B %Y', '%d %b %Y']:
                            try:
                                portfolio_date = pd.to_datetime(cell_str, format=date_format).date()
                                break
                            except:
                                try:
                                    # Try pandas flexible parsing
                                    portfolio_date = pd.to_datetime(cell_str).date()
                                    break
                                except:
                                    continue
                    if portfolio_date:
                        break
                if portfolio_date:
                    break
        
        if portfolio_date is None:
            return {
                'success': False,
                'filename': uploaded_file.name,
                'error': 'Could not extract date from Excel file. Please ensure the file contains a date near the "Portfolio Holdings Report" header or in the filename (format: YYYYMMDD).'
            }
        
        # Find header row starting from the row after "Portfolio Holdings Report"
        header_row = None
        for idx in range(report_row + 1, len(df_raw)):
            row = df_raw.iloc[idx]
            row_str = ' '.join([str(val).lower() for val in row.values if pd.notna(val)])
            if 'instrument description' in row_str and 'total quantity' in row_str:
                header_row = idx
                break
        
        if header_row is None:
            return {
                'success': False,
                'filename': uploaded_file.name,
                'error': 'Could not find header row (with "Instrument Description" and "Total Quantity") after "Portfolio Holdings Report".'
            }
        
        # Read with header row
        df = pd.read_excel(uploaded_file, header=header_row)
        
        # Map columns by name (from Excel structure)
        instrument_col = 'Instrument Description'
        quantity_col = 'Total Quantity'
        currency_col = 'Currency'
        unit_cost_col = 'Unit'  # Unit Cost (net)
        total_cost_col = 'Total Cost'
        price_col = 'Price'
        total_value_col = 'Total Value'
        exchange_rate_col = 'Exchange'  # Exchange Rate
        move_percent_col = 'Move (%)'
        portfolio_percent_col = 'Portfolio'  # Portfolio (%)
        profit_loss_col = 'Profit/Loss'
        annual_income_col = 'Annual'  # Annual Income (R)
        
        # Check if columns exist
        col_names = list(df.columns)
        missing_cols = []
        for col_name, col_var in [
            (instrument_col, 'Instrument Description'),
            (quantity_col, 'Total Quantity'),
            (currency_col, 'Currency'),
            (unit_cost_col, 'Unit'),
            (total_cost_col, 'Total Cost'),
            (price_col, 'Price'),
            (total_value_col, 'Total Value'),
        ]:
            if col_name not in col_names:
                missing_cols.append(col_var)
        
        if missing_cols:
            return {
                'success': False,
                'filename': uploaded_file.name,
                'error': f'Missing required columns: {", ".join(missing_cols)}',
                'available_columns': col_names[:30]
            }
        
        # Clear existing portfolio data for this month/year (not just the specific date)
        deleted_count = InvestecJsePortfolio.objects.filter(
            year=portfolio_date.year,
            month=portfolio_date.month
        ).delete()[0]
        
        # Prepare data for bulk creation
        portfolios_to_create = []
        errors = []
        
        for index, row in df.iterrows():
            try:
                # Get instrument description
                instrument_desc = row[instrument_col]
                if pd.isna(instrument_desc) or str(instrument_desc).strip() == '':
                    continue
                
                # Extract company and share_code from "ABSA GROUP LIMITED (ABG)"
                instrument_str = str(instrument_desc).strip()
                company = ''
                share_code = ''
                
                # Pattern: "COMPANY NAME (CODE)"
                match = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', instrument_str)
                if match:
                    company = match.group(1).strip()
                    share_code = match.group(2).strip()
                else:
                    # If no parentheses, use full string as company
                    company = instrument_str
                    share_code = ''
                
                # Get quantity - only process rows with quantity
                quantity_value = row[quantity_col]
                if pd.isna(quantity_value):
                    continue  # Skip rows without quantity (totals/headers)
                
                # Convert quantity - handle string values like " 910.00" or "1 959.00"
                if isinstance(quantity_value, str):
                    quantity_value = quantity_value.replace(' ', '').replace(',', '').strip()
                
                try:
                    quantity = Decimal(str(quantity_value))
                    if quantity == 0:
                        continue  # Skip rows with zero quantity
                except (InvalidOperation, ValueError):
                    continue  # Skip invalid quantity rows (totals/headers)
                
                # Get other required fields
                currency = str(row[currency_col])[:10] if not pd.isna(row[currency_col]) else 'ZAR'
                
                # Unit cost
                unit_cost_value = row[unit_cost_col]
                if pd.isna(unit_cost_value):
                    errors.append(f'Row {index + header_row + 2}: Unit cost is missing')
                    continue
                try:
                    unit_cost = Decimal(str(unit_cost_value))
                except (InvalidOperation, ValueError):
                    errors.append(f'Row {index + header_row + 2}: Invalid unit cost: {unit_cost_value}')
                    continue
                
                # Total cost
                total_cost_value = row[total_cost_col]
                if pd.isna(total_cost_value):
                    errors.append(f'Row {index + header_row + 2}: Total cost is missing')
                    continue
                try:
                    total_cost = Decimal(str(total_cost_value))
                except (InvalidOperation, ValueError):
                    errors.append(f'Row {index + header_row + 2}: Invalid total cost: {total_cost_value}')
                    continue
                
                # Price
                price_value = row[price_col]
                if pd.isna(price_value):
                    errors.append(f'Row {index + header_row + 2}: Price is missing')
                    continue
                try:
                    price = Decimal(str(price_value))
                except (InvalidOperation, ValueError):
                    errors.append(f'Row {index + header_row + 2}: Invalid price: {price_value}')
                    continue
                
                # Total value
                total_value_value = row[total_value_col]
                if pd.isna(total_value_value):
                    errors.append(f'Row {index + header_row + 2}: Total value is missing')
                    continue
                try:
                    total_value = Decimal(str(total_value_value))
                except (InvalidOperation, ValueError):
                    errors.append(f'Row {index + header_row + 2}: Invalid total value: {total_value_value}')
                    continue
                
                # Optional fields
                exchange_rate = None
                if exchange_rate_col in col_names and not pd.isna(row[exchange_rate_col]):
                    try:
                        exchange_rate = Decimal(str(row[exchange_rate_col]))
                    except (InvalidOperation, ValueError):
                        pass
                
                move_percent = None
                if move_percent_col in col_names and not pd.isna(row[move_percent_col]):
                    try:
                        move_percent = Decimal(str(row[move_percent_col]))
                    except (InvalidOperation, ValueError):
                        pass
                
                portfolio_percent = None
                if portfolio_percent_col in col_names and not pd.isna(row[portfolio_percent_col]):
                    try:
                        portfolio_percent = Decimal(str(row[portfolio_percent_col]))
                    except (InvalidOperation, ValueError):
                        pass
                
                profit_loss = None
                if profit_loss_col in col_names and not pd.isna(row[profit_loss_col]):
                    try:
                        profit_loss = Decimal(str(row[profit_loss_col]))
                    except (InvalidOperation, ValueError):
                        pass
                
                # Annual Income (R)
                annual_income_zar = None
                if annual_income_col in col_names and not pd.isna(row[annual_income_col]):
                    try:
                        annual_income_zar = Decimal(str(row[annual_income_col]))
                    except (InvalidOperation, ValueError):
                        pass
                
                portfolios_to_create.append(
                    InvestecJsePortfolio(
                        date=portfolio_date,
                        year=portfolio_date.year,
                        month=portfolio_date.month,
                        day=portfolio_date.day,
                        company=company[:100],
                        share_code=share_code[:20],
                        quantity=quantity,
                        currency=currency,
                        unit_cost=unit_cost,
                        total_cost=total_cost,
                        price=price,
                        total_value=total_value,
                        exchange_rate=exchange_rate,
                        move_percent=move_percent,
                        portfolio_percent=portfolio_percent,
                        profit_loss=profit_loss,
                        annual_income_zar=annual_income_zar,
                    )
                )
            except Exception as e:
                errors.append(f'Row {index + header_row + 2}: {str(e)}')
                continue
        
        # Bulk create portfolios
        created_count = 0
        if portfolios_to_create:
            with transaction.atomic():
                created_instances = InvestecJsePortfolio.objects.bulk_create(
                    portfolios_to_create,
                    ignore_conflicts=False
                )
                created_count = len(created_instances)
                # Sync share name mappings with company and share_code from holdings.
                # For each unique (share_code, company) in the uploaded holdings:
                #   - If a mapping with this share_code exists → update company
                #   - If NO mapping with this share_code exists → create one with share_name = company
                #     (user can later correct share_name to match transaction names via the Excel upload)
                seen = set()
                new_mappings = []
                for p in portfolios_to_create:
                    if not (p.share_code and p.company):
                        continue
                    key = p.share_code
                    if key in seen:
                        continue
                    seen.add(key)
                    updated = InvestecJseShareNameMapping.objects.filter(share_code=p.share_code).update(
                        company=p.company,
                    )
                    if updated == 0:
                        # No mapping exists for this share_code yet — create one
                        # Use company name as the initial share_name (placeholder; user can rename)
                        if not InvestecJseShareNameMapping.objects.filter(share_name=p.company).exists():
                            new_mappings.append(
                                InvestecJseShareNameMapping(
                                    share_name=p.company,
                                    company=p.company,
                                    share_code=p.share_code,
                                )
                            )
                if new_mappings:
                    InvestecJseShareNameMapping.objects.bulk_create(new_mappings, ignore_conflicts=True)
        
        # Retrieve and serialize the created data
        portfolio_data = []
        if created_count > 0:
            # Query the created portfolios by date and company/share_code to get full data with IDs
            portfolios = InvestecJsePortfolio.objects.filter(date=portfolio_date).order_by('company', 'share_code')
            portfolio_data = InvestecJsePortfolioSerializer(portfolios, many=True).data
        
        # Prepare response
        return {
            'success': True,
            'filename': uploaded_file.name,
            'message': f'Successfully imported {created_count} portfolio holdings',
            'date': str(portfolio_date),
            'year': portfolio_date.year,
            'month': portfolio_date.month,
            'deleted_previous': deleted_count,
            'total_rows': len(df),
            'created': created_count,
            'errors': len(errors),
            'data': portfolio_data,
            'error_details': errors[:50] if errors else []  # Limit to first 50 errors
        }
        
    except pd.errors.EmptyDataError:
        return {
            'success': False,
            'filename': uploaded_file.name,
            'error': 'The Excel file is empty.'
        }
    except Exception as e:
        return {
            'success': False,
            'filename': uploaded_file.name,
            'error': f'Error processing file: {str(e)}'
        }


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def portfolio_upload_view(request):
    """
    API endpoint to upload Excel file(s) and import portfolio holdings.
    
    Accepts POST request with 'file' or 'files' field(s) containing Excel file(s).
    Date is extracted from each Excel file itself.
    
    For each file, all portfolio data for that month/year will be deleted before importing.
    This ensures only one version per month is kept.
    
    Returns import statistics, imported data, and any errors encountered for each file.
    """
    # Get files - support both 'file' (single) and 'files' (multiple)
    uploaded_files = []
    if 'files' in request.FILES:
        uploaded_files = request.FILES.getlist('files')
    elif 'file' in request.FILES:
        uploaded_files = [request.FILES['file']]
    else:
        return Response(
            {'error': 'No file provided. Please upload an Excel file (use "file" or "files" field).'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    if not uploaded_files:
        return Response(
            {'error': 'No file provided. Please upload an Excel file.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Process each file
    results = []
    total_created = 0
    total_deleted = 0
    total_errors = 0
    
    for uploaded_file in uploaded_files:
        result = process_portfolio_file(uploaded_file)
        results.append(result)
        
        if result.get('success'):
            total_created += result.get('created', 0)
            total_deleted += result.get('deleted_previous', 0)
            total_errors += result.get('errors', 0)
    
    # Prepare aggregated response
    successful_files = [r for r in results if r.get('success')]
    failed_files = [r for r in results if not r.get('success')]
    
    response_data = {
        'success': len(failed_files) == 0,
        'total_files': len(uploaded_files),
        'successful_files': len(successful_files),
        'failed_files': len(failed_files),
        'total_created': total_created,
        'total_deleted': total_deleted,
        'total_errors': total_errors,
        'files': results,
    }
    
    status_code = status.HTTP_201_CREATED if total_created > 0 else status.HTTP_200_OK
    if failed_files:
        status_code = status.HTTP_207_MULTI_STATUS  # Multi-Status if some files failed
    
    return Response(response_data, status=status_code)


# ------------------------------------------------
# Share Name Mapping
# ------------------------------------------------

@api_view(['GET'])
def mapping_list_view(request):
    """
    API endpoint to list all share name mappings (share_name, company, share_code).
    """
    mappings = InvestecJseShareNameMapping.objects.all().order_by('share_name')
    serializer = InvestecJseShareNameMappingSerializer(mappings, many=True)
    return Response({
        'count': len(serializer.data),
        'results': serializer.data,
    })


@api_view(['GET'])
def unmapped_share_names_view(request):
    """
    List share names that appear in transactions but do not match share_name, share_name2, or share_name3
    in the Share codes/names/company mapping table.
    """
    mappings = InvestecJseShareNameMapping.objects.all().values('share_name', 'share_name2', 'share_name3')
    mapped_names = set()
    for m in mappings:
        if m['share_name']:
            mapped_names.add(m['share_name'])
        if m['share_name2']:
            mapped_names.add(m['share_name2'])
        if m['share_name3']:
            mapped_names.add(m['share_name3'])

    transaction_share_names = (
        InvestecJseTransaction.objects.exclude(share_name='')
        .exclude(share_name__isnull=True)
        .values_list('share_name', flat=True)
        .distinct()
    )
    unmapped = sorted(set(transaction_share_names) - mapped_names)
    return Response({
        'count': len(unmapped),
        'share_names': unmapped,
    })


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def mapping_upload_view(request):
    """
    API endpoint to upload Excel file and import share name mappings.
    
    Accepts POST request with 'file' field containing Excel file.
    Expected columns: Share_Name, Company, Share_Code
    Company and Share_Code are optional.
    
    Returns import statistics and any errors encountered.
    """
    if 'file' not in request.FILES:
        return Response(
            {'error': 'No file provided. Please upload an Excel file.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    uploaded_file = request.FILES['file']
    
    # Validate file extension
    if not uploaded_file.name.endswith(('.xlsx', '.xls')):
        return Response(
            {'error': 'Invalid file format. Please upload an Excel file (.xlsx or .xls).'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        # Read Excel file
        df = pd.read_excel(uploaded_file)
        
        # Normalize column names
        df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_').str.replace('-', '_')
        
        # Map column names
        share_name_col = None
        share_name2_col = None
        share_name3_col = None
        company_col = None
        share_code_col = None
        
        for col in df.columns:
            col_lower = col.lower()
            if col_lower in ('share_name2', 'sharename2'):
                share_name2_col = col
            elif col_lower in ('share_name3', 'sharename3'):
                share_name3_col = col
            elif 'share_name' in col_lower or 'sharename' in col_lower:
                share_name_col = col
            elif 'company' in col_lower:
                company_col = col
            elif 'share_code' in col_lower or 'sharecode' in col_lower or 'code' in col_lower:
                share_code_col = col
        
        if not share_name_col:
            return Response(
                {
                    'error': 'Missing required column: Share_Name',
                    'available_columns': list(df.columns)
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        def _col_val(row, col, maxlen=100):
            if col and not pd.isna(row[col]):
                v = str(row[col]).strip()[:maxlen]
                return v if v not in ('nan', '') else ''
            return ''

        # Prepare data for bulk create/update/delete
        mappings_to_create = []
        mappings_to_update = []
        ids_to_delete = set()
        errors = []
        
        for index, row in df.iterrows():
            try:
                share_name = str(row[share_name_col]).strip() if not pd.isna(row[share_name_col]) else ''
                if not share_name:
                    continue
                
                company = _col_val(row, company_col)
                share_code = _col_val(row, share_code_col, maxlen=20)
                share_name2 = _col_val(row, share_name2_col) or None
                share_name3 = _col_val(row, share_name3_col) or None
                
                # If share_code exists in DB, update that row's share_name and company (or merge into existing share_name row)
                if share_code:
                    existing_by_code = InvestecJseShareNameMapping.objects.filter(share_code=share_code).first()
                    if existing_by_code:
                        existing_by_share_name = InvestecJseShareNameMapping.objects.filter(share_name=share_name).first()
                        if existing_by_share_name and existing_by_share_name.id != existing_by_code.id:
                            # Another row already has this share_name: merge into it and remove the row found by share_code
                            if company:
                                existing_by_share_name.company = company
                            existing_by_share_name.share_code = share_code
                            if share_name2 is not None:
                                existing_by_share_name.share_name2 = share_name2
                            if share_name3 is not None:
                                existing_by_share_name.share_name3 = share_name3
                            mappings_to_update.append(existing_by_share_name)
                            ids_to_delete.add(existing_by_code.id)
                        else:
                            # Same row or no conflict: update fields on the share_code row
                            existing_by_code.share_name = share_name
                            if company:
                                existing_by_code.company = company
                            if share_name2 is not None:
                                existing_by_code.share_name2 = share_name2
                            if share_name3 is not None:
                                existing_by_code.share_name3 = share_name3
                            if existing_by_code.id not in ids_to_delete:
                                mappings_to_update.append(existing_by_code)
                        continue
                # Else try to update by share_name
                try:
                    existing = InvestecJseShareNameMapping.objects.get(share_name=share_name)
                    if existing.id in ids_to_delete:
                        continue
                    if company:
                        existing.company = company
                    if share_code:
                        existing.share_code = share_code
                    if share_name2 is not None:
                        existing.share_name2 = share_name2
                    if share_name3 is not None:
                        existing.share_name3 = share_name3
                    mappings_to_update.append(existing)
                except InvestecJseShareNameMapping.DoesNotExist:
                    mappings_to_create.append(
                        InvestecJseShareNameMapping(
                            share_name=share_name,
                            share_name2=share_name2,
                            share_name3=share_name3,
                            company=company if company else None,
                            share_code=share_code if share_code else None,
                        )
                    )
            except Exception as e:
                errors.append(f'Row {index + 2}: {str(e)}')
                continue
        
        # Bulk create, update, and delete
        created_count = 0
        updated_count = 0
        deleted_count = 0
        
        with transaction.atomic():
            if mappings_to_create:
                created_instances = InvestecJseShareNameMapping.objects.bulk_create(
                    mappings_to_create,
                    ignore_conflicts=False
                )
                created_count = len(created_instances)
            
            if ids_to_delete:
                deleted_count, _ = InvestecJseShareNameMapping.objects.filter(id__in=ids_to_delete).delete()
            
            if mappings_to_update:
                # Deduplicate by id so each row only updated once; exclude any we're deleting
                seen_ids = set(ids_to_delete)
                unique_updates = []
                for m in mappings_to_update:
                    if m.id not in seen_ids:
                        seen_ids.add(m.id)
                        unique_updates.append(m)
                if unique_updates:
                    update_fields = ['company', 'share_code', 'share_name', 'share_name2', 'share_name3']
                    InvestecJseShareNameMapping.objects.bulk_update(unique_updates, update_fields)
                    updated_count = len(unique_updates)
        
        parts = []
        if created_count:
            parts.append(f'{created_count} created')
        if updated_count:
            parts.append(f'{updated_count} updated')
        if deleted_count:
            parts.append(f'{deleted_count} merged/removed')
        response_data = {
            'success': True,
            'message': 'Successfully imported mappings' + (f': {", ".join(parts)}' if parts else ''),
            'created': created_count,
            'updated': updated_count,
            'deleted': deleted_count,
            'errors': len(errors),
        }
        
        if errors:
            response_data['error_details'] = errors[:50]
            if len(errors) > 50:
                response_data['error_details'].append(f'... and {len(errors) - 50} more errors')
        
        return Response(response_data, status=status.HTTP_201_CREATED if created_count > 0 or updated_count > 0 or deleted_count > 0 else status.HTTP_200_OK)
        
    except pd.errors.EmptyDataError:
        return Response(
            {'error': 'The Excel file is empty.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        return Response(
            {'error': f'Error processing file: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def export_companies_view(request):
    """
    API endpoint to export all unique companies from portfolios.
    
    Returns a list of companies with their share codes.
    """
    companies = InvestecJsePortfolio.objects.values('company', 'share_code').distinct().order_by('company')
    
    # Format as list
    companies_list = [
        {
            'company': item['company'],
            'share_code': item['share_code']
        }
        for item in companies
    ]
    
    return Response({
        'count': len(companies_list),
        'companies': companies_list
    })


@api_view(['GET'])
def export_share_names_view(request):
    """
    API endpoint to export all unique share names from transactions.
    
    Returns a list of share names.
    """
    share_names = InvestecJseTransaction.objects.exclude(
        share_name=''
    ).exclude(
        share_name__isnull=True
    ).values_list('share_name', flat=True).distinct().order_by('share_name')
    
    share_names_list = list(share_names)
    
    return Response({
        'count': len(share_names_list),
        'share_names': share_names_list
    })


@api_view(['GET'])
def export_mapping_view(request):
    """
    Stream the share name mapping as an Excel file download directly to the browser.
    Columns: Share_Name, Share_Name2, Share_Name3, Company, Share_Code
    """
    try:
        mappings = InvestecJseShareNameMapping.objects.all().order_by('share_name')
        rows = [
            {
                'Share_Name': m.share_name or '',
                'Share_Name2': m.share_name2 or '',
                'Share_Name3': m.share_name3 or '',
                'Company': m.company or '',
                'Share_Code': m.share_code or '',
            }
            for m in mappings
        ]
        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine='openpyxl')
        buf.seek(0)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'Share_Codes_Names_Company_{timestamp}.xlsx'
        response = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        return Response(
            {'error': f'Error exporting mappings: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def export_transactions_view(request):
    """
    API endpoint to export all InvestecJseTransaction data to Excel file.
    
    Exports all transactions to an Excel file in investec/exports (under MEDIA_ROOT).
    """
    try:
        # Get all transactions
        transactions = InvestecJseTransaction.objects.all().order_by('-date', '-created_at')
        
        # Convert to list of dictionaries
        transactions_data = []
        for txn in transactions:
            # Convert timezone-aware datetimes to timezone-naive for Excel compatibility
            created_at = txn.created_at.replace(tzinfo=None) if txn.created_at else None
            updated_at = txn.updated_at.replace(tzinfo=None) if txn.updated_at else None
            
            transactions_data.append({
                'Date': txn.date,
                'Year': txn.year,
                'Month': txn.month,
                'Day': txn.day,
                'Account Number': txn.account_number,
                'Description': txn.description,
                'Share Name': txn.share_name,
                'Type': txn.type,
                'Quantity': float(txn.quantity) if txn.quantity else None,
                'Value': float(txn.value) if txn.value else None,
                'Value Per Share': float(txn.value_per_share) if txn.value_per_share else None,
                'Value Calculated': float(txn.value_calculated) if txn.value_calculated else None,
                'Dividend TTM': float(txn.dividend_ttm) if txn.dividend_ttm else None,
                'Created At': created_at,
                'Updated At': updated_at,
            })
        
        # Create DataFrame
        df = pd.DataFrame(transactions_data)
        
        # Export to investec/exports under MEDIA_ROOT
        exports_dir = os.path.join(settings.MEDIA_ROOT, 'investec', 'exports')
        os.makedirs(exports_dir, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'InvestecJseTransaction_Export_{timestamp}.xlsx'
        filepath = os.path.join(exports_dir, filename)
        
        # Export to Excel
        df.to_excel(filepath, index=False, engine='openpyxl')
        
        return Response({
            'success': True,
            'message': f'Successfully exported {len(transactions_data)} transactions to Excel',
            'filename': filename,
            'filepath': filepath,
            'count': len(transactions_data)
        })
        
    except Exception as e:
        return Response(
            {'error': f'Error exporting transactions: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ------------------------------------------------
# Investec Private Bank (Bank Account & Transactions)
# ------------------------------------------------

@api_view(['GET'])
def bank_account_list_view(request):
    """
    List all Investec Private Bank accounts (for dropdown/filter).
    """
    accounts = InvestecBankAccount.objects.all().order_by('account_number')
    data = [
        {
            'id': a.id,
            'account_id': a.account_id,
            'account_number': a.account_number,
            'account_name': a.account_name or a.reference_name or '',
        }
        for a in accounts
    ]
    return Response({'results': data})


@api_view(['GET'])
def bank_transaction_list_view(request):
    """
    Search Investec Private Bank transactions across all accounts.
    Query params:
    - amount: exact or partial match (e.g. "100" or "100.50")
    - description: case-insensitive substring match
    - date_from: YYYY-MM-DD
    - date_to: YYYY-MM-DD
    - account: account_id or account_number (filter by account)
    - limit, offset: pagination (default limit=100, offset=0)
    """
    queryset = _bank_transactions_queryset(request)

    total_count = queryset.count()
    limit = min(int(request.query_params.get('limit', 100)), 500)
    offset = int(request.query_params.get('offset', 0))
    page = queryset[offset:offset + limit]

    results = [
        {
            'id': t.id,
            'account_id': t.account_id,
            'account_number': t.account.account_number,
            'account_name': t.account.account_name or t.account.reference_name or '',
            'posting_date': t.posting_date.isoformat() if t.posting_date else None,
            'transaction_date': t.transaction_date.isoformat() if t.transaction_date else None,
            'type': t.type,
            'amount': str(t.amount),
            'description': t.description or '',
            'status': t.status,
            'running_balance': str(t.running_balance) if t.running_balance is not None else None,
        }
        for t in page
    ]
    return Response({
        'count': total_count,
        'limit': limit,
        'offset': offset,
        'results': results,
    })


def _bank_transactions_queryset(request):
    """Build the same filtered queryset as bank_transaction_list_view (for list and export)."""
    qs = InvestecBankTransaction.objects.select_related('account').all().order_by('-posting_date', '-posted_order', 'id')
    description = (request.query_params.get('description') or '').strip()
    if description:
        qs = qs.filter(description__icontains=description)
    amount_param = (request.query_params.get('amount') or '').strip()
    if amount_param:
        try:
            qs = qs.filter(amount=Decimal(amount_param))
        except (InvalidOperation, ValueError):
            pass
    date_from = request.query_params.get('date_from')
    if date_from:
        d = parse_date(date_from)
        if d:
            qs = qs.filter(posting_date__gte=d)
    date_to = request.query_params.get('date_to')
    if date_to:
        d = parse_date(date_to)
        if d:
            qs = qs.filter(posting_date__lte=d)
    account_param = (request.query_params.get('account') or '').strip()
    if account_param:
        qs = qs.filter(
            Q(account__account_id=account_param) | Q(account__account_number=account_param)
        )
    return qs


@api_view(['GET'])
def bank_transaction_export_view(request):
    """
    Export Investec bank transaction search results to Excel.
    Same query params as bank/transactions/ (description, amount, date_from, date_to, account).
    Returns Excel file; max 50_000 rows.
    """
    try:
        queryset = _bank_transactions_queryset(request)
        max_export = 50000
        page = list(queryset[:max_export])
        rows = [
            {
                'Posting Date': t.posting_date.isoformat() if t.posting_date else '',
                'Transaction Date': t.transaction_date.isoformat() if t.transaction_date else '',
                'Account Number': t.account.account_number,
                'Account Name': (t.account.account_name or t.account.reference_name or ''),
                'Type': t.type or '',
                'Amount': float(t.amount),
                'Description': t.description or '',
                'Status': t.status or '',
                'Running Balance': float(t.running_balance) if t.running_balance is not None else None,
            }
            for t in page
        ]
        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine='openpyxl')
        buf.seek(0)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'Investec_Bank_Transactions_{timestamp}.xlsx'
        response = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        return Response(
            {'error': f'Error exporting: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(['GET'])
def bank_sync_status_view(request):
    """Return last Investec bank sync time for display in the portal."""
    log = InvestecBankSyncLog.objects.filter(key='default').first()
    last_synced_at = log.last_synced_at.isoformat() if log and log.last_synced_at else None
    return Response({'last_synced_at': last_synced_at})


@api_view(['POST'])
def bank_sync_trigger_view(request):
    """
    Trigger sync from Investec API. Only fetches from last_synced_at date to today (incremental).
    If never synced, uses last 180 days. Returns created/updated counts and new last_synced_at.
    """
    log = InvestecBankSyncLog.objects.filter(key='default').first()
    to_date = timezone.now().date()
    if log and log.last_synced_at:
        from_date = log.last_synced_at.date()
    else:
        from_date = to_date - timedelta(days=180)

    result = run_investec_bank_sync(
        from_date=from_date,
        to_date=to_date,
        include_pending=False,
        account_filter=None,
        dry_run=False,
        update_sync_log=True,
    )

    if result.get('error'):
        return Response({'error': result['error']}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        'created': result['created'],
        'updated': result['updated'],
        'last_synced_at': result.get('last_synced_at'),
        'from_date': from_date.isoformat(),
        'to_date': to_date.isoformat(),
    })
