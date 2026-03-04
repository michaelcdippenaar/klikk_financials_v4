"""
Comparison services for Trail Balance and Profit & Loss reports.
"""
import logging
from decimal import Decimal
from datetime import date

from django.db.models import Sum

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_cube.models import XeroTrailBalance
from ..models import (
    XeroTrailBalanceReport, XeroTrailBalanceReportLine, TrailBalanceComparison,
    XeroProfitAndLossReport, XeroProfitAndLossReportLine, ProfitAndLossComparison
)

logger = logging.getLogger(__name__)


def compare_trail_balance(tenant_id, report_id=None, report_date=None, tolerance=Decimal('0.01')):
    """
    Compare Xero trail balance report with our database trail balance.
    
    Args:
        tenant_id: Xero tenant ID
        report_id: ID of XeroTrailBalanceReport to compare (optional)
        report_date: Date of report to compare (optional, uses latest if not provided)
        tolerance: Tolerance for differences (default 0.01)
    
    Returns:
        dict: Comparison results with statistics
    """
    print("[PROCESS] compare_trail_balance")
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
    
    # Delete existing comparisons for this report
    TrailBalanceComparison.objects.filter(report=report).delete()
    
    # Get all report lines (excluding header/summary rows)
    # Include null accounts in response (use account_id, not account_code)
    report_lines = XeroTrailBalanceReportLine.objects.filter(
        report=report,
        row_type__in=['Row', None, '']  # Only actual data rows
    )  # Don't exclude null accounts - we want to list them in response
    
    # Get database trail balance for the same date
    # Trail balance is stored by year/month, so we need to get the month of the report
    report_year = report.report_date.year
    report_month = report.report_date.month
    
    # Get aggregated trail balance from our database
    db_trail_balance = XeroTrailBalance.objects.filter(
        organisation=organisation,
        year=report_year,
        month=report_month
    ).values('account').annotate(
        total_amount=Sum('amount')
    )
    
    # Create a dictionary for quick lookup
    db_balances = {
        item['account']: Decimal(str(item['total_amount']))
        for item in db_trail_balance
    }
    
    # Compare each report line with database
    comparisons = []
    matches = 0
    mismatches = 0
    missing_in_db = 0
    missing_in_xero = 0
    null_accounts_count = 0
    
    null_accounts_list = []  # List to store null account information for response
    
    for line in report_lines:
        # Handle null accounts - include them in response but don't create DB records
        if not line.account:
            # Track null accounts separately
            null_accounts_count += 1
            null_accounts_list.append({
                'account_code': line.account_code or '',
                'account_name': line.account_name or '',
                'xero_value': str(line.value),
                'db_value': '0',
                'difference': str(line.value),
                'match_status': 'null_account',
                'notes': f"Account not found in database. Account code: {line.account_code}, Account name: {line.account_name}"
            })
            continue
        
        xero_value = line.value
        db_value = db_balances.get(line.account.account_id, Decimal('0'))
        
        # Calculate difference
        difference = xero_value - db_value
        abs_difference = abs(difference)
        
        # Determine match status
        if abs_difference <= tolerance:
            match_status = 'match'
            matches += 1
        else:
            if db_value == 0:
                match_status = 'missing_in_db'
                missing_in_db += 1
            else:
                match_status = 'mismatch'
                mismatches += 1
        
        # Create comparison record
        comparison = TrailBalanceComparison.objects.create(
            report=report,
            account=line.account,
            xero_value=xero_value,
            db_value=db_value,
            difference=difference,
            match_status=match_status
        )
        comparisons.append(comparison)
    
    # Find accounts in DB that are missing in Xero report
    xero_account_ids = {line.account.account_id for line in report_lines if line.account}
    for account_id, db_value in db_balances.items():
        if account_id not in xero_account_ids and abs(db_value) > tolerance:
            try:
                account = XeroAccount.objects.get(account_id=account_id)
                TrailBalanceComparison.objects.create(
                    report=report,
                    account=account,
                    xero_value=Decimal('0'),
                    db_value=db_value,
                    difference=-db_value,
                    match_status='missing_in_xero',
                    notes=f"Account exists in DB but not in Xero report"
                )
                missing_in_xero += 1
            except XeroAccount.DoesNotExist:
                continue
    
    total_comparisons = len(comparisons) + missing_in_xero
    
    return {
        'success': True,
        'message': f"Comparison completed for report dated {report.report_date}",
        'report_id': report.id,
        'report_date': report.report_date,
        'statistics': {
            'total_comparisons': total_comparisons,
            'matches': matches,
            'mismatches': mismatches,
            'missing_in_db': missing_in_db,
            'missing_in_xero': missing_in_xero,
            'null_accounts_count': null_accounts_count,  # Count of null accounts
            'match_percentage': (matches / total_comparisons * 100) if total_comparisons > 0 else 0
        },
        'comparisons': comparisons,
        'null_accounts': null_accounts_list  # List of null accounts with details
    }


def compare_profit_loss(tenant_id, report_id=None, tolerance=Decimal('0.01')):
    """
    Compare Xero Profit and Loss report with our database trail balance.
    Compares income and expense accounts per month for each period.
    
    Args:
        tenant_id: Xero tenant ID
        report_id: ID of XeroProfitAndLossReport to compare (optional, uses latest if not provided)
        tolerance: Tolerance for differences (default 0.01)
    
    Returns:
        dict: Comparison results with statistics per period
    """
    print("[PROCESS] compare_profit_loss")
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    # Get the report
    if report_id:
        report = XeroProfitAndLossReport.objects.get(id=report_id, organisation=organisation)
    else:
        # Get latest report
        report = XeroProfitAndLossReport.objects.filter(
            organisation=organisation
        ).order_by('-to_date', '-imported_at').first()
    
    if not report:
        raise ValueError("No P&L report found. Please import a report first.")
    
    # Delete existing comparisons for this report
    ProfitAndLossComparison.objects.filter(report=report).delete()
    
    # Get income and expense account types
    income_expense_types = ['REVENUE', 'EXPENSE']
    
    # Get report lines for income/expense accounts only (exclude headers and summary rows)
    report_lines = XeroProfitAndLossReportLine.objects.filter(
        report=report,
        account__type__in=income_expense_types,
        row_type='Row'
    ).exclude(account__isnull=True)
    
    # Calculate period dates
    period_dates = []
    current_date = report.from_date
    for i in range(report.periods):
        period_dates.append(current_date)
        if i < report.periods - 1:
            # Add one month
            if current_date.month == 12:
                current_date = date(current_date.year + 1, 1, 1)
            else:
                current_date = date(current_date.year, current_date.month + 1, 1)
    
    # Compare each period
    comparisons = []
    period_stats = {}
    
    for period_idx in range(report.periods):
        period_date = period_dates[period_idx]
        period_year = period_date.year
        period_month = period_date.month
        
        period_matches = 0
        period_mismatches = 0
        period_missing_in_db = 0
        period_missing_in_xero = 0
        
        # Get database trail balance for this period
        db_trail_balance = XeroTrailBalance.objects.filter(
            organisation=organisation,
            account__type__in=income_expense_types,
            year=period_year,
            month=period_month
        ).values('account').annotate(
            total_amount=Sum('amount')
        )
        
        # Create dictionary for quick lookup
        db_balances = {
            item['account']: Decimal(str(item['total_amount']))
            for item in db_trail_balance
        }
        
        # Compare each report line for this period
        for line in report_lines:
            if not line.account:
                continue
            
            # Get Xero value for this period
            period_key = f'period_{period_idx}'
            xero_value = Decimal(str(line.period_values.get(period_key, '0')))
            
            # Get database value
            db_raw = db_balances.get(line.account.account_id, Decimal('0'))
            # Normalise to P&L display convention.
            # REVENUE: DB is usually credit-negative; invert so income displays as positive.
            # EXPENSE: keep DB sign as-is so contra/credit expense lines stay negative and
            # match Xero report presentation.
            if line.account.type == 'EXPENSE':
                db_value = db_raw
            elif line.account.type == 'REVENUE':
                db_value = -db_raw if db_raw != 0 else db_raw  # display as positive income
            else:
                db_value = db_raw
            
            # Calculate difference
            difference = xero_value - db_value
            abs_difference = abs(difference)
            
            # Determine match status
            if abs_difference <= tolerance:
                match_status = 'match'
                period_matches += 1
            else:
                if db_value == 0:
                    match_status = 'missing_in_db'
                    period_missing_in_db += 1
                else:
                    match_status = 'mismatch'
                    period_mismatches += 1
            
            # Create comparison record
            comparison = ProfitAndLossComparison.objects.create(
                report=report,
                account=line.account,
                period_index=period_idx,
                period_date=period_date,
                xero_value=xero_value,
                db_value=db_value,
                difference=difference,
                match_status=match_status
            )
            comparisons.append(comparison)
        
        # Find accounts in DB that are missing in Xero report for this period
        xero_account_ids = {line.account.account_id for line in report_lines if line.account}
        for account_id, db_raw in db_balances.items():
            if account_id not in xero_account_ids and abs(db_raw) > tolerance:
                try:
                    account = XeroAccount.objects.get(account_id=account_id)
                    if account.type == 'EXPENSE':
                        db_value = db_raw
                    elif account.type == 'REVENUE':
                        db_value = -db_raw if db_raw != 0 else db_raw
                    else:
                        db_value = db_raw
                    ProfitAndLossComparison.objects.create(
                        report=report,
                        account=account,
                        period_index=period_idx,
                        period_date=period_date,
                        xero_value=Decimal('0'),
                        db_value=db_value,
                        difference=-db_value,
                        match_status='missing_in_xero',
                        notes=f"Account exists in DB but not in Xero report for period {period_idx}"
                    )
                    period_missing_in_xero += 1
                except XeroAccount.DoesNotExist:
                    continue
        
        total_period_comparisons = len([c for c in comparisons if c.period_index == period_idx]) + period_missing_in_xero
        
        period_stats[period_idx] = {
            'period_date': period_date.strftime('%Y-%m-%d'),
            'total_comparisons': total_period_comparisons,
            'matches': period_matches,
            'mismatches': period_mismatches,
            'missing_in_db': period_missing_in_db,
            'missing_in_xero': period_missing_in_xero,
            'match_percentage': (period_matches / total_period_comparisons * 100) if total_period_comparisons > 0 else 0
        }
    
    # Build per-period list of exceptions (non-match comparisons) for API response
    period_exceptions = {}
    for c in comparisons:
        if c.match_status == 'match':
            continue
        pid = str(c.period_index)
        if pid not in period_exceptions:
            period_exceptions[pid] = []
        period_exceptions[pid].append({
            'account_code': c.account.code or '',
            'account_name': c.account.name or '',
            'xero_value': str(c.xero_value),
            'db_value': str(c.db_value),
            'difference': str(c.difference),
            'status': c.match_status,
        })

    # Overall statistics
    total_comparisons = len(comparisons)
    total_matches = sum(stats['matches'] for stats in period_stats.values())
    total_mismatches = sum(stats['mismatches'] for stats in period_stats.values())

    return {
        'success': True,
        'message': f"P&L comparison completed for report {report.from_date} to {report.to_date}",
        'report_id': report.id,
        'from_date': report.from_date,
        'to_date': report.to_date,
        'periods': report.periods,
        'period_stats': period_stats,
        'period_exceptions': period_exceptions,
        'overall_statistics': {
            'total_comparisons': total_comparisons,
            'total_matches': total_matches,
            'total_mismatches': total_mismatches,
            'overall_match_percentage': (total_matches / total_comparisons * 100) if total_comparisons > 0 else 0
        },
        'comparisons': comparisons
    }

