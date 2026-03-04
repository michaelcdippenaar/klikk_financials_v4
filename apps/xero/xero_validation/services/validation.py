"""
Validation services for balance sheet accounts.
"""
import logging
from decimal import Decimal

from django.db.models import Sum, Q

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_cube.models import XeroTrailBalance
from ..models import XeroTrailBalanceReport, XeroTrailBalanceReportLine
from ..helpers.balance_sheet_excel_parser import parse_balance_sheet_excel
from .imports import import_trail_balance_from_xero
from .comparisons import compare_trail_balance
from .exports import export_all_line_items_to_csv
from .income_statement import add_income_statement_to_trail_balance_report

logger = logging.getLogger(__name__)


def validate_balance_sheet_complete(
    tenant_id,
    report_date=None,
    user=None,
    tolerance=Decimal('0.01'),
    import_trail_balance_only=False,
    compare_only=False,
    validate_only=False,
    export_line_items=False,
    add_income_statement=False,
    target_account_code='960'
):
    """
    Combined validation endpoint that can run all steps or individual steps.
    
    Args:
        tenant_id: Xero tenant ID
        report_date: Date for the report (defaults to today)
        user: User object for API authentication
        tolerance: Tolerance for differences (default 0.01)
        import_trail_balance_only: If True, only import trail balance and return
        compare_only: If True, only compare trail balance (requires existing report)
        validate_only: If True, only validate balance sheet accounts (requires existing report)
        export_line_items: If True, export line items to CSV
        add_income_statement: If True, add income statement to report
        target_account_code: Account code for income statement (default '960')
    
    Returns:
        dict: Combined results with all steps executed
    """
    print("[PROCESS] validate_balance_sheet_complete")
    results = {
        'success': True,
        'steps_executed': [],
        'report_id': None,
        'report_date': None,
        'stats': {}
    }
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    report = None
    
    # Step 1: Import Trail Balance (unless compare_only or validate_only)
    if not compare_only and not validate_only:
        # Delete old reports before importing new one
        old_reports = XeroTrailBalanceReport.objects.filter(organisation=organisation)
        old_count = old_reports.count()
        if old_count > 0:
            print(f"[VALIDATION] Deleting {old_count} old trail balance reports for tenant {tenant_id}")
            logger.info(f"Deleting {old_count} old trail balance reports for tenant {tenant_id}")
            # Delete related comparisons and lines first (CASCADE should handle this, but being explicit)
            from ..models import XeroTrailBalanceReportLine, TrailBalanceComparison
            for report in old_reports:
                TrailBalanceComparison.objects.filter(report=report).delete()
                XeroTrailBalanceReportLine.objects.filter(report=report).delete()
            deleted_count = old_reports.delete()[0]
            print(f"[VALIDATION] Deleted {deleted_count} old trail balance reports")
            logger.info(f"Deleted {deleted_count} old trail balance reports for tenant {tenant_id}")
        
        logger.info(f"[VALIDATION] Step 1: Importing trail balance for tenant {tenant_id}")
        import_result = import_trail_balance_from_xero(
            tenant_id=tenant_id,
            report_date=report_date,
            user=user
        )
        report = import_result['report']
        results['report_id'] = report.id
        results['report_date'] = report.report_date
        results['steps_executed'].append('import_trail_balance')
        results['stats']['import'] = {
            'lines_created': import_result.get('lines_created', 0),
            'is_new': import_result.get('is_new', False)
        }
        
        if import_trail_balance_only:
            results['message'] = f"Trail balance imported successfully. Report ID: {report.id}"
            return results
    
    # Get report if not already imported
    if not report:
        if report_date:
            report = XeroTrailBalanceReport.objects.filter(
                organisation=organisation,
                report_date=report_date
            ).order_by('-imported_at').first()
        else:
            report = XeroTrailBalanceReport.objects.filter(
                organisation=organisation
            ).order_by('-report_date', '-imported_at').first()
        
        if not report:
            raise ValueError("No trail balance report found. Please import a report first or set import_trail_balance_only=False")
        
        results['report_id'] = report.id
        results['report_date'] = report.report_date
    
    # Step 2: Compare Trail Balance (unless validate_only)
    if not validate_only:
        logger.info(f"[VALIDATION] Step 2: Comparing trail balance for report {report.id}")
        compare_result = compare_trail_balance(
            tenant_id=tenant_id,
            report_id=report.id,
            tolerance=tolerance
        )
        results['steps_executed'].append('compare_trail_balance')
        results['stats']['compare'] = compare_result.get('statistics', {})
        
        if compare_only:
            results['message'] = f"Trail balance comparison completed. Report ID: {report.id}"
            return results
    
    # Step 3: Validate Balance Sheet Accounts
    logger.info(f"[VALIDATION] Step 3: Validating balance sheet accounts for report {report.id}")
    validate_result = validate_balance_sheet_accounts(
        tenant_id=tenant_id,
        report_id=report.id,
        tolerance=tolerance
    )
    results['steps_executed'].append('validate_balance_sheet')
    results['stats']['validate'] = {
        'overall_status': validate_result.get('overall_status', 'unknown'),
        'statistics': validate_result.get('statistics', {}),
        'validations': validate_result.get('validations', [])
    }
    
    if validate_only:
        results['message'] = f"Balance sheet validation completed. Report ID: {report.id}"
        results['overall_status'] = validate_result.get('overall_status', 'unknown')
        return results
    
    # Step 4: Export Line Items (optional)
    if export_line_items:
        logger.info(f"[VALIDATION] Step 4: Exporting line items for report {report.id}")
        try:
            export_result = export_all_line_items_to_csv(report_id=report.id)
            results['steps_executed'].append('export_line_items')
            results['stats']['export'] = {
                'lines_exported': export_result.get('lines_exported', 0),
                'file_path': export_result.get('file_path'),
                'filename': export_result.get('filename')
            }
        except Exception as e:
            logger.error(f"Error exporting line items: {str(e)}")
            results['stats']['export'] = {'error': str(e)}
    
    # Step 5: Add Income Statement (optional)
    if add_income_statement:
        logger.info(f"[VALIDATION] Step 5: Adding income statement to report {report.id}")
        try:
            income_result = add_income_statement_to_trail_balance_report(
                report_id=report.id,
                tenant_id=tenant_id,
                target_account_code=target_account_code
            )
            results['steps_executed'].append('add_income_statement')
            results['stats']['income_statement'] = {
                'lines_created': income_result.get('lines_created', 0),
                'pnl_value': str(income_result.get('pnl_value', 0)),
                'revenue_total': str(income_result.get('revenue_total', 0)),
                'expense_total': str(income_result.get('expense_total', 0))
            }
        except Exception as e:
            logger.error(f"Error adding income statement: {str(e)}")
            results['stats']['income_statement'] = {'error': str(e)}
    
    # Overall success message
    results['message'] = f"Validation completed successfully. Report ID: {report.id}, Date: {report.report_date}"
    results['overall_status'] = validate_result.get('overall_status', 'unknown')
    
    return results


def validate_balance_sheet_accounts(tenant_id, report_id=None, report_date=None, tolerance=Decimal('0.01')):
    """
    Validate balance sheet accounts from Xero trail balance report against database trail balance.
    Validates across ALL periods up to the report date (cumulative YTD comparison).
    
    Only validates balance sheet accounts (Asset, Liability, Equity).
    If balance sheet accounts are in balance, income statement will be correct.
    
    Args:
        tenant_id: Xero tenant ID
        report_id: ID of XeroTrailBalanceReport to validate (optional)
        report_date: Date of report to validate (optional, uses latest if not provided)
        tolerance: Tolerance for differences (default 0.01)
    
    Returns:
        dict: Validation results with statistics and detailed comparisons including account UUIDs
    """
    print("[PROCESS] validate_balance_sheet")
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    # Get the report
    if report_id:
        report = XeroTrailBalanceReport.objects.get(id=report_id, organisation=organisation)
    elif report_date:
        report = XeroTrailBalanceReport.objects.filter(
            organisation=organisation,
            report_date=report_date
        ).first()
    else:
        # Get latest report
        report = XeroTrailBalanceReport.objects.filter(
            organisation=organisation
        ).order_by('-report_date', '-imported_at').first()
    
    if not report:
        raise ValueError("No trail balance report found. Please import a report first.")
    
    # Balance sheet account groupings in Xero
    # Filter by grouping field: ASSET, LIABILITY, EQUITY
    BALANCE_SHEET_GROUPINGS = ['ASSET', 'LIABILITY', 'EQUITY']
    
    # Get all report lines with balance sheet accounts only
    report_lines = XeroTrailBalanceReportLine.objects.filter(
        report=report,
        account__isnull=False
    ).exclude(account_code='').select_related('account')
    
    # Filter to only balance sheet accounts using grouping field
    balance_sheet_lines = []
    for line in report_lines:
        if line.account and line.account.grouping:
            account_grouping = line.account.grouping.upper()
            if account_grouping in BALANCE_SHEET_GROUPINGS:
                balance_sheet_lines.append(line)
    
    logger.info(f"Found {len(balance_sheet_lines)} balance sheet accounts in report (filtered from {len(report_lines)} total lines)")
    
    # Build a lookup map for Xero account UUIDs from parsed_json
    # Also build a set of all account_ids found in the parsed data (for bank accounts without codes)
    xero_uuid_map = {}  # Maps account_code -> account_id_uuid from parsed data
    xero_uuid_by_code_map = {}  # Also map by account_code for lookup
    xero_account_ids_in_parsed = set()  # Set of all account_ids found in parsed_json
    if report.parsed_json:
        for row in report.parsed_json:
            account_code = row.get('account_code', '').strip()
            account_id_uuid = row.get('account_id_uuid', '').strip()
            if account_id_uuid:
                xero_account_ids_in_parsed.add(account_id_uuid)
                if account_code:
                    xero_uuid_map[account_code] = account_id_uuid
                    xero_uuid_by_code_map[account_code] = account_id_uuid
        logger.debug(f"[VALIDATION] Built UUID map with {len(xero_uuid_map)} entries from parsed_json")
        logger.debug(f"[VALIDATION] Found {len(xero_account_ids_in_parsed)} account_ids in parsed_json")
        if len(xero_uuid_map) > 0:
            sample_codes = list(xero_uuid_map.keys())[:10]
            logger.debug(f"[VALIDATION] Sample account codes in parsed data: {sample_codes}")
    
    # Get database trail balance - cumulative YTD up to report date
    report_year = report.report_date.year
    report_month = report.report_date.month
    
    # Get ALL periods up to and including the report month (cumulative)
    # Filter by grouping field to only get balance sheet accounts
    # Get all records where:
    # - year < report_year, OR
    # - year == report_year AND month <= report_month
    db_trail_balance = XeroTrailBalance.objects.filter(
        organisation=organisation,
        account__grouping__in=BALANCE_SHEET_GROUPINGS
    ).filter(
        Q(year__lt=report_year) | 
        (Q(year=report_year) & Q(month__lte=report_month))
    ).values('account').annotate(
        total_amount=Sum('amount')
    )
    
    # Create a dictionary for quick lookup: account_id (string) -> cumulative balance
    # The 'account' field in values() returns the account_id (primary key) as a string
    db_balances = {}
    for item in db_trail_balance:
        account_id = item['account']  # This is the account_id string (primary key)
        db_balances[account_id] = Decimal(str(item['total_amount']))
    
    logger.info(f"Found {len(db_balances)} accounts with database balances")
    
    # Calculate cumulative P&L (Profit & Loss) for Retained Earnings adjustment
    # This includes ALL REVENUE and EXPENSE accounts for ALL PERIODS up to the report date
    # 
    # HOW P&L IS CALCULATED:
    # In XeroTrailBalance, amounts are stored with their natural sign:
    #   - REVENUE accounts: amounts are typically stored as credits (negative values in amount field)
    #   - EXPENSE accounts: amounts are typically stored as debits (positive values in amount field)
    # 
    # Therefore: P&L = revenue_total + expense_total
    #   - If revenue_total is negative (credits) and expense_total is positive (debits),
    #     adding them gives: (-revenue) + (+expense) = Net Income
    #   - Example: Revenue = -1000 (credit), Expense = 500 (debit)
    #     P&L = -1000 + 500 = -500 (net loss) OR
    #     P&L = 1000 + (-500) = 500 (net profit) depending on sign convention
    #
    # The cumulative P&L represents the total net income/loss across ALL periods
    
    # Calculate total REVENUE for ALL PERIODS up to and including the report date
    # Filter directly by grouping to ensure we get ALL REVENUE accounts
    revenue_total = XeroTrailBalance.objects.filter(
        organisation=organisation,
        account__grouping='REVENUE'
    ).filter(
        Q(year__lt=report_year) | 
        (Q(year=report_year) & Q(month__lte=report_month))
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    # Calculate total EXPENSE for ALL PERIODS up to and including the report date
    # Filter directly by grouping to ensure we get ALL EXPENSE accounts
    expense_total = XeroTrailBalance.objects.filter(
        organisation=organisation,
        account__grouping='EXPENSE'
    ).filter(
        Q(year__lt=report_year) | 
        (Q(year=report_year) & Q(month__lte=report_month))
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    # P&L = Revenue + Expense (cumulative across ALL periods)
    # This works because of how amounts are stored in XeroTrailBalance
    cumulative_pnl = revenue_total + expense_total
    
    logger.info(
        f"Calculated cumulative P&L for ALL PERIODS up to {report_year}-{report_month:02d}: "
        f"P&L = {cumulative_pnl} (Revenue: {revenue_total}, Expense: {expense_total}, Formula: revenue_total + expense_total)"
    )
    
    # Compare each balance sheet account
    validations = []
    matches = 0
    mismatches = 0
    missing_in_db = 0
    missing_in_xero = 0
    total_xero_value = Decimal('0')
    total_db_value = Decimal('0')
    
    for line in balance_sheet_lines:
        account = line.account
        # Use YTD debit - credit for comparison (debit and credit fields contain YTD values)
        xero_value = line.debit - line.credit
        # Use account_id (primary key) instead of id
        db_value = db_balances.get(account.account_id, Decimal('0'))  # Cumulative from DB
        original_db_value = db_value  # Store original for logging
        
        # For Retained Earnings (account 960), add cumulative P&L to db_value
        # Retained Earnings = Previous Retained Earnings + Net Income (P&L)
        # Skip if this is the P&L entry itself (has db_value set and name contains "P&L")
        is_pnl_entry = (line.db_value is not None and line.account_name and 'P&L' in line.account_name)
        
        # Check if this is account 960 (Retained Earnings) - check both line.account_code and account.code
        # Also check if account name contains "retained" (case-insensitive)
        line_account_code = (line.account_code or '').strip()
        account_code = (account.code or '').strip() if account.code else ''
        account_name_lower = (account.name or '').lower() if account.name else ''
        
        is_retained_earnings = (
            not is_pnl_entry and 
            (line_account_code == '960' or account_code == '960' or 
             (account.grouping == 'EQUITY' and 'retained' in account_name_lower))
        )
        
        if is_retained_earnings:
            # This is the Retained Earnings account, add cumulative P&L to its balance
            db_value = db_value + cumulative_pnl
            logger.info(f"Added P&L ({cumulative_pnl}) to Retained Earnings account {line_account_code or account_code or account.account_id}: db_value = {db_value} (was {original_db_value})")
        
        total_xero_value += xero_value
        total_db_value += db_value
        
        # Calculate difference
        difference = xero_value - db_value
        abs_difference = abs(difference)
        
        # Determine validation status
        if abs_difference <= tolerance:
            status = 'match'
            matches += 1
        else:
            if db_value == 0:
                status = 'missing_in_db'
                missing_in_db += 1
            else:
                status = 'mismatch'
                mismatches += 1
        
        validation_entry = {
            'account_id': account.account_id,
            'account_code': account.code,
            'account_name': account.name,
            'account_grouping': account.grouping,
            'account_type': account.type,
            'xero_debit': str(line.debit),
            'xero_credit': str(line.credit),
            'xero_value': str(xero_value),  # debit - credit (YTD)
            'db_value': str(db_value),
            'difference': str(difference),
            'abs_difference': str(abs_difference),
            'status': status
        }
        
        # Add P&L info if this is Retained Earnings
        if is_retained_earnings:
            validation_entry['cumulative_pnl_added'] = str(cumulative_pnl)
            validation_entry['db_value_before_pnl'] = str(original_db_value)
        
        validations.append(validation_entry)
    
    # Find accounts in DB that are missing in Xero report
    # Check both balance_sheet_lines (linked accounts) and parsed_json (all accounts including bank accounts without codes)
    xero_account_ids = {line.account.account_id for line in balance_sheet_lines if line.account}
    # Also include account_ids from parsed_json (for bank accounts that might not be linked)
    xero_account_ids.update(xero_account_ids_in_parsed)
    
    for account_id, db_value in db_balances.items():
        # Skip if account is found in Xero report (either in balance_sheet_lines or parsed_json)
        if account_id in xero_account_ids:
            continue
            
        if abs(db_value) > tolerance:
            try:
                account = XeroAccount.objects.get(account_id=account_id)
                account_grouping = (account.grouping or '').upper()
                # Only include if it's a balance sheet account
                if account_grouping in BALANCE_SHEET_GROUPINGS:
                    # For bank accounts without codes, use account_id as account_code display
                    account_code_display = account.code if account.code else account.account_id
                    
                    validations.append({
                        'account_id': account.account_id,
                        'account_code': account_code_display,  # Use account_id if code is empty (for bank accounts)
                        'account_name': account.name,
                        'account_grouping': account.grouping,
                        'account_type': account.type,
                        'xero_value': '0',
                        'db_value': str(db_value),
                        'difference': str(-db_value),
                        'abs_difference': str(abs(db_value)),
                        'status': 'missing_in_xero'
                    })
                    missing_in_xero += 1
                    total_db_value += db_value
            except XeroAccount.DoesNotExist:
                continue
    
    total_difference = total_xero_value - total_db_value
    total_abs_difference = abs(total_difference)
    
    # Overall validation result
    overall_status = 'pass' if total_abs_difference <= tolerance and mismatches == 0 and missing_in_db == 0 else 'fail'
    
    return {
        'success': True,
        'overall_status': overall_status,
        'message': f"Balance sheet validation {'PASSED' if overall_status == 'pass' else 'FAILED'} for report dated {report.report_date}",
        'report_id': report.id,
        'report_date': report.report_date,
        'cumulative_pnl': str(cumulative_pnl),  # Include P&L in response for debugging
        'statistics': {
            'total_accounts_validated': len(validations),
            'matches': matches,
            'mismatches': mismatches,
            'missing_in_db': missing_in_db,
            'missing_in_xero': missing_in_xero,
            'match_percentage': (matches / len(validations) * 100) if validations else 0,
            'total_xero_value': str(total_xero_value),
            'total_db_value': str(total_db_value),
            'total_difference': str(total_difference),
            'total_abs_difference': str(total_abs_difference),
            'tolerance': str(tolerance),
            'cumulative_pnl': str(cumulative_pnl)
        },
        'validations': validations
    }


def validate_balance_sheet_from_excel(tenant_id, file_path, report_date=None, tolerance=Decimal('0.01'), **parser_kwargs):
    """
    Validate balance sheet data from a Xero Balance Sheet Excel export against the database.

    Uses the same DB logic as validate_balance_sheet_accounts: cumulative XeroTrailBalance
    for ASSET/LIABILITY/EQUITY to report_date, plus Retained Earnings (960) adjustment from P&L.

    Args:
        tenant_id: Xero tenant ID
        file_path: Path to the .xlsx Balance Sheet file
        report_date: Report date (required if not present in Excel)
        tolerance: Tolerance for numeric comparison (default 0.01)
        **parser_kwargs: Optional kwargs passed to parse_balance_sheet_excel

    Returns:
        dict: Same shape as validate_balance_sheet_accounts (validations, statistics, overall_status, etc.)
    """
    from datetime import date

    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")

    parsed = parse_balance_sheet_excel(file_path, **parser_kwargs)
    excel_rows = parsed.get('rows') or []
    report_date = report_date or parsed.get('report_date')
    if not report_date:
        raise ValueError("report_date is required (not found in Excel and not provided)")

    if isinstance(report_date, str):
        from datetime import datetime
        report_date = datetime.strptime(report_date, '%Y-%m-%d').date()

    BALANCE_SHEET_GROUPINGS = ['ASSET', 'LIABILITY', 'EQUITY']

    # Resolve each Excel row to XeroAccount (by code then name), filter to balance sheet only
    excel_balances = {}  # account_id -> value
    excel_account_names = {}  # account_id -> account_name for output
    unmatched_rows = []

    for row in excel_rows:
        account_name = (row.get('account_name') or '').strip()
        account_code = (row.get('account_code') or '').strip()
        value = row.get('value') or Decimal('0')
        if isinstance(value, str):
            try:
                value = Decimal(value)
            except Exception:
                value = Decimal('0')

        account = None
        if account_code:
            account = (
                XeroAccount.objects.filter(organisation=organisation, code=account_code)
                .first()
            )
        if not account and account_name:
            account = (
                XeroAccount.objects.filter(organisation=organisation)
                .filter(
                    Q(name__iexact=account_name) | Q(name__icontains=account_name)
                )
                .first()
            )
        if not account:
            unmatched_rows.append({'account_name': account_name, 'account_code': account_code, 'value': value})
            continue

        grouping = (account.grouping or '').upper()
        if grouping not in BALANCE_SHEET_GROUPINGS:
            continue

        excel_balances[account.account_id] = value
        excel_account_names[account.account_id] = account_name

    logger.info(
        "Excel parsed: %d rows, %d matched to balance sheet accounts, %d unmatched",
        len(excel_rows), len(excel_balances), len(unmatched_rows),
    )

    # DB: cumulative balances to report_date (same as validate_balance_sheet_accounts)
    report_year = report_date.year
    report_month = report_date.month

    db_trail_balance = XeroTrailBalance.objects.filter(
        organisation=organisation,
        account__grouping__in=BALANCE_SHEET_GROUPINGS
    ).filter(
        Q(year__lt=report_year) |
        (Q(year=report_year) & Q(month__lte=report_month))
    ).values('account').annotate(total_amount=Sum('amount'))

    db_balances = {}
    for item in db_trail_balance:
        account_id = item['account']
        db_balances[account_id] = Decimal(str(item['total_amount']))

    revenue_total = XeroTrailBalance.objects.filter(
        organisation=organisation,
        account__grouping='REVENUE'
    ).filter(
        Q(year__lt=report_year) |
        (Q(year=report_year) & Q(month__lte=report_month))
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    expense_total = XeroTrailBalance.objects.filter(
        organisation=organisation,
        account__grouping='EXPENSE'
    ).filter(
        Q(year__lt=report_year) |
        (Q(year=report_year) & Q(month__lte=report_month))
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    cumulative_pnl = revenue_total + expense_total

    validations = []
    matches = 0
    mismatches = 0
    missing_in_db = 0
    missing_in_excel = 0
    total_xero_value = Decimal('0')
    total_db_value = Decimal('0')

    for account_id, xero_value in excel_balances.items():
        try:
            account = XeroAccount.objects.get(account_id=account_id)
        except XeroAccount.DoesNotExist:
            continue

        db_value = db_balances.get(account_id, Decimal('0'))
        original_db_value = db_value

        account_name_lower = (account.name or '').lower()
        is_retained_earnings = (
            (account.code == '960' or (account.grouping == 'EQUITY' and 'retained' in account_name_lower))
        )
        if is_retained_earnings:
            db_value = db_value + cumulative_pnl

        total_xero_value += xero_value
        total_db_value += db_value
        difference = xero_value - db_value
        abs_difference = abs(difference)

        if abs_difference <= tolerance:
            status = 'match'
            matches += 1
        else:
            if db_value == 0:
                status = 'missing_in_db'
                missing_in_db += 1
            else:
                status = 'mismatch'
                mismatches += 1

        validations.append({
            'account_id': account_id,
            'account_code': account.code or '',
            'account_name': account.name or excel_account_names.get(account_id, ''),
            'account_grouping': account.grouping,
            'account_type': account.type,
            'xero_value': str(xero_value),
            'db_value': str(db_value),
            'difference': str(difference),
            'abs_difference': str(abs_difference),
            'status': status,
        })
        if is_retained_earnings:
            validations[-1]['cumulative_pnl_added'] = str(cumulative_pnl)
            validations[-1]['db_value_before_pnl'] = str(original_db_value)

    # DB accounts not in Excel
    for account_id, db_value in db_balances.items():
        if account_id in excel_balances:
            continue
        if abs(db_value) <= tolerance:
            continue
        try:
            account = XeroAccount.objects.get(account_id=account_id)
            if (account.grouping or '').upper() not in BALANCE_SHEET_GROUPINGS:
                continue
            validations.append({
                'account_id': account_id,
                'account_code': account.code or account_id,
                'account_name': account.name,
                'account_grouping': account.grouping,
                'account_type': account.type,
                'xero_value': '0',
                'db_value': str(db_value),
                'difference': str(-db_value),
                'abs_difference': str(abs(db_value)),
                'status': 'missing_in_xero',
            })
            missing_in_excel += 1
            total_db_value += db_value
        except XeroAccount.DoesNotExist:
            pass

    total_difference = total_xero_value - total_db_value
    total_abs_difference = abs(total_difference)
    overall_status = 'pass' if total_abs_difference <= tolerance and mismatches == 0 and missing_in_db == 0 else 'fail'

    result = {
        'success': True,
        'overall_status': overall_status,
        'message': f"Balance sheet Excel validation {'PASSED' if overall_status == 'pass' else 'FAILED'} for report dated {report_date}",
        'report_date': report_date,
        'file_path': file_path,
        'cumulative_pnl': str(cumulative_pnl),
        'statistics': {
            'total_accounts_validated': len(validations),
            'matches': matches,
            'mismatches': mismatches,
            'missing_in_db': missing_in_db,
            'missing_in_xero': missing_in_excel,
            'match_percentage': (matches / len(validations) * 100) if validations else 0,
            'total_xero_value': str(total_xero_value),
            'total_db_value': str(total_db_value),
            'total_difference': str(total_difference),
            'total_abs_difference': str(total_abs_difference),
            'tolerance': str(tolerance),
            'cumulative_pnl': str(cumulative_pnl),
            'excel_rows_parsed': len(excel_rows),
            'excel_rows_matched': len(excel_balances),
            'excel_rows_unmatched': len(unmatched_rows),
        },
        'validations': validations,
    }
    if unmatched_rows:
        result['unmatched_excel_rows'] = [
            {'account_name': r['account_name'], 'account_code': r.get('account_code'), 'value': str(r.get('value'))}
            for r in unmatched_rows[:50]
        ]
    return result

