"""
Import services for Trail Balance and Profit & Loss reports.
"""
import json
import logging
from datetime import datetime

from django.utils import timezone

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi, serialize_model
from apps.xero.xero_metadata.models import XeroAccount
from ..models import XeroTrailBalanceReport, XeroTrailBalanceReportLine
from ..helpers.trial_balance_parser import parse_trial_balance_dict, parse_trial_balance_report
from ..helpers.profit_loss_parser import parse_profit_loss_report, parse_profit_loss_report_multi
from ..helpers.service_helpers import convert_decimals_to_strings

logger = logging.getLogger(__name__)


def import_trial_balance_from_file(tenant_id, json_path, report_date=None):
    """
    Import a Trial Balance that has already been exported from Xero and saved as a JSON text file.

    Args:
        tenant_id: Xero tenant ID
        json_path: path to the JSON file (Trial Balance as returned by Xero)
        report_date: optional override; if None, use ReportDate from JSON

    Returns:
        dict with success, report, lines_created
    """
    organisation = XeroTenant.objects.get(tenant_id=tenant_id)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    reports = data.get("Reports", [])
    if not reports:
        raise ValueError("No Reports section in JSON")

    report_meta = reports[0]

    # Use Xero's ReportDate if not provided (e.g. "20 November 2025")
    if report_date is None:
        try:
            report_date = datetime.strptime(
                report_meta.get("ReportDate"), "%d %B %Y"
            ).date()
        except Exception:
            report_date = timezone.now().date()

    # Parse the raw data to get parsed JSON
    parsed_rows = parse_trial_balance_dict(data)
    
    # Convert Decimal values to strings for JSON serialization
    parsed_json_serializable = convert_decimals_to_strings(parsed_rows)
    
    # Create XeroTrailBalanceReport with raw_data and parsed_json stored
    report = XeroTrailBalanceReport.objects.create(
        organisation=organisation,
        report_date=report_date,
        report_type=report_meta.get("ReportType", "TrialBalance"),
        raw_data=data,
        parsed_json=parsed_json_serializable,
    )
    lines_created = 0

    for row in parsed_rows:
        account = None

        # Prefer UUID from Attributes
        if row["account_id_uuid"]:
            try:
                account = XeroAccount.objects.get(
                    organisation=organisation,
                    account_id=row["account_id_uuid"],
                )
            except XeroAccount.DoesNotExist:
                account = None

        # Fallback: account code
        if not account and row["account_code"]:
            try:
                account = XeroAccount.objects.get(
                    organisation=organisation,
                    code=row["account_code"],
                )
            except XeroAccount.DoesNotExist:
                account = (
                    XeroAccount.objects.filter(
                        organisation=organisation,
                        code__iexact=row["account_code"].strip(),
                    ).first()
                    or None
                )

        XeroTrailBalanceReportLine.objects.create(
            report=report,
            account=account,
            account_code=row["account_code"],
            account_name=row["account_name"],
            account_type=None,  # Xero TB JSON doesn't give type directly
            debit=row["debit"],
            credit=row["credit"],
            value=row["value"],
            row_type=row["row_type"],
            raw_cell_data={"row": row["raw_row"]},
        )
        lines_created += 1

    logger.info(f"Imported {lines_created} lines from file for {report_date}")

    return {
        'success': True,
        'message': f"Successfully imported trail balance from file for {report_date}",
        'report': report,
        'lines_created': lines_created,
        'is_new': True
    }


def import_trail_balance_from_xero(tenant_id, report_date=None, user=None):
    """
    Import trail balance report from Xero API.
    
    Args:
        tenant_id: Xero tenant ID
        report_date: Date for the report (defaults to today)
        user: User object for API authentication
    
    Returns:
        dict: Result with status, message, and report instance
    """
    print("[PROCESS] import_trail_balance")
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    if report_date is None:
        report_date = timezone.now().date()
    
    # Check if report already exists for this date and delete it
    existing_report = XeroTrailBalanceReport.objects.filter(
        organisation=organisation,
        report_date=report_date
    ).first()
    
    if existing_report:
        print(f"[PROCESS] Report already exists for {report_date}, deleting existing report")
        logger.info(f"Report already exists for {report_date}, deleting existing report")
        # Delete related comparisons and lines first
        from ..models import XeroTrailBalanceReportLine, TrailBalanceComparison
        TrailBalanceComparison.objects.filter(report=existing_report).delete()
        XeroTrailBalanceReportLine.objects.filter(report=existing_report).delete()
        existing_report.delete()
        print(f"[PROCESS] Deleted existing report for {report_date}")
        logger.info(f"Deleted existing report for {report_date}")
    
    # Get user from credentials if not provided
    if not user:
        from apps.xero.xero_auth.models import XeroClientCredentials
        credentials = XeroClientCredentials.objects.filter(active=True).first()
        if not credentials:
            raise ValueError("No active Xero credentials found and no user provided")
        user = credentials.user
    
    # Initialize API client
    api_client = XeroApiClient(user, tenant_id=tenant_id)
    xero_api = XeroAccountingApi(api_client, tenant_id)
    
    # Fetch trail balance report from Xero
    date_string = report_date.strftime("%Y-%m-%d")
    logger.info(f"Fetching trail balance report from Xero for date: {date_string}")
    
    try:
        # Call Xero API to get trial balance report
        trial_balance_obj = xero_api.api_client.get_report_trial_balance(
            tenant_id,
            date=date_string
        )
        
        # Serialize the response
        data = serialize_model(trial_balance_obj)
        
        # Create report instance with raw data
        reports = data.get('Reports', [])
        if not reports:
            raise ValueError("No report data returned from Xero")
        
        report_meta = reports[0]
        
        # Parse the raw data to get parsed JSON
        parsed_rows = parse_trial_balance_dict(data)
        
        # Convert Decimal values to strings for JSON serialization
        parsed_json_serializable = convert_decimals_to_strings(parsed_rows)
        
        # Create report instance with raw data and parsed JSON
        report = XeroTrailBalanceReport.objects.create(
            organisation=organisation,
            report_date=report_date,
            report_type=report_meta.get('ReportType', 'TrialBalance'),
            raw_data=data,
            parsed_json=parsed_json_serializable
        )
        
        # Parse and create report lines
        parse_stats = parse_trial_balance_report(report)
        lines_created = parse_stats.get('lines_created', 0)
        
        logger.info(f"Imported {lines_created} lines from Xero trail balance report for {report_date}")
        
        return {
            'success': True,
            'message': f"Successfully imported trail balance report for {report_date}",
            'report': report,
            'lines_created': lines_created,
            'is_new': True
        }
        
    except Exception as e:
        logger.error(f"Error importing trail balance from Xero: {str(e)}", exc_info=True)
        raise Exception(f"Failed to import trail balance from Xero: {str(e)}") from e


def import_profit_loss_from_xero(tenant_id, from_date, to_date, periods=11, timeframe='MONTH', user=None, report_from_date=None):
    """
    Import Profit and Loss report from Xero API.
    
    Args:
        tenant_id: Xero tenant ID
        from_date: Start date for the Xero API call (date object or YYYY-MM-DD string)
        to_date: End date for the Xero API call (date object or YYYY-MM-DD string)
        periods: Number of comparison periods (default 11 → 12 columns with timeframe=MONTH)
        timeframe: MONTH, QUARTER, or YEAR
        user: User object for API authentication
        report_from_date: Start date for the stored report (for comparison alignment).
                          Defaults to from_date. Use to store a FY-start date while the
                          API call targets only the last month of the FY.
    
    Returns:
        dict: Result with status, message, and report instance
    """
    print("[PROCESS] import_profit_loss")
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    # Convert dates to date objects if strings
    if isinstance(from_date, str):
        from_date = timezone.datetime.strptime(from_date, '%Y-%m-%d').date()
    if isinstance(to_date, str):
        to_date = timezone.datetime.strptime(to_date, '%Y-%m-%d').date()
    if isinstance(report_from_date, str):
        report_from_date = timezone.datetime.strptime(report_from_date, '%Y-%m-%d').date()
    if report_from_date is None:
        report_from_date = from_date
    
    # Get user from credentials if not provided
    if not user:
        from apps.xero.xero_auth.models import XeroClientCredentials
        credentials = XeroClientCredentials.objects.filter(active=True).first()
        if not credentials:
            raise ValueError("No active Xero credentials found and no user provided")
        user = credentials.user
    
    # Initialize API client
    api_client = XeroApiClient(user, tenant_id=tenant_id)
    xero_api = XeroAccountingApi(api_client, tenant_id)
    
    # Format dates for API
    from_date_str = from_date.strftime("%Y-%m-%d")
    to_date_str = to_date.strftime("%Y-%m-%d")
    
    logger.info(f"Fetching P&L report from Xero: API {from_date_str} to {to_date_str}, {periods} periods (report from_date={report_from_date})")
    
    try:
        # Call Xero API to get Profit and Loss report
        pnl_data = xero_api.profit_and_loss().get(
            from_date=from_date_str,
            to_date=to_date_str,
            periods=periods,
            timeframe=timeframe
        )
        
        # Debug: Check the structure of the returned data
        if pnl_data and isinstance(pnl_data, dict):
            reports = pnl_data.get("Reports", [])
            if reports:
                report_data = reports[0]
                rows = report_data.get("Rows", [])
                print(f"[IMPORT] P&L API returned {len(rows)} top-level rows")
                # Check first data row to see cell structure
                for row in rows[:3]:
                    if row.get("RowType") in ["Row", "SummaryRow"]:
                        cells = row.get("Cells", [])
                        print(f"[IMPORT] Sample row has {len(cells)} cells")
                        if cells:
                            print(f"[IMPORT]   Cell 0 (account): {cells[0].get('Value', '')}")
                            if len(cells) > 1:
                                print(f"[IMPORT]   Cell 1 (period 0?): {cells[1].get('Value', '')}")
                            if len(cells) > 2:
                                print(f"[IMPORT]   Cell 2 (period 1?): {cells[2].get('Value', '')}")
                        break
        
        # Parse and create report — store with report_from_date (FY start) for comparison alignment
        report = parse_profit_loss_report(
            organisation=organisation,
            data=pnl_data,
            from_date=report_from_date,
            to_date=to_date,
            periods=periods + 1  # periods=11 means 12 months (0-11)
        )
        
        lines_created = report.lines.count()
        
        logger.info(f"Imported P&L report with {lines_created} lines for {report_from_date} to {to_date}")
        
        return {
            'success': True,
            'message': f"Successfully imported P&L report for {report_from_date} to {to_date}",
            'report': report,
            'lines_created': lines_created,
            'is_new': True
        }
        
    except Exception as e:
        logger.error(f"Error importing P&L report from Xero: {str(e)}", exc_info=True)
        raise Exception(f"Failed to import P&L report from Xero: {str(e)}") from e


def import_profit_loss_from_xero_reconciliation(tenant_id, from_date, to_date, api_call_plans, user=None):
    """
    Import P&L for reconciliation using pre-built API call plans (31-day anchors).
    Makes one or more Xero API calls and merges results into a single report.

    Args:
        tenant_id: Xero tenant ID
        from_date: Report start date (FY start)
        to_date: Report end date (capped to current month)
        api_call_plans: List of (anchor_from_date, anchor_to_date, n_periods, batch_months)
                        from reconciliation._build_pnl_api_call_plans
        user: Optional user for API auth

    Returns:
        dict: { 'report': XeroProfitAndLossReport, 'lines_created': int, 'message': str }
    """
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")

    if isinstance(from_date, str):
        from_date = timezone.datetime.strptime(from_date, '%Y-%m-%d').date()
    if isinstance(to_date, str):
        to_date = timezone.datetime.strptime(to_date, '%Y-%m-%d').date()

    if not user:
        from apps.xero.xero_auth.models import XeroClientCredentials
        creds = XeroClientCredentials.objects.filter(active=True).first()
        if not creds:
            raise ValueError("No active Xero credentials found and no user provided")
        user = creds.user

    api_client = XeroApiClient(user, tenant_id=tenant_id)
    xero_api = XeroAccountingApi(api_client, tenant_id)

    api_responses = []
    for anchor_from, anchor_to, n_periods, batch_months in api_call_plans:
        from_str = anchor_from.strftime("%Y-%m-%d")
        to_str = anchor_to.strftime("%Y-%m-%d")
        logger.info(f"Fetching P&L from Xero: {from_str} to {to_str}, periods={n_periods}")
        pnl_data = xero_api.profit_and_loss().get(
            from_date=from_str,
            to_date=to_str,
            periods=n_periods,
            timeframe="MONTH",
        )
        api_responses.append((pnl_data, batch_months))

    report = parse_profit_loss_report_multi(organisation, api_responses, from_date, to_date)
    lines_created = report.lines.count()
    logger.info(f"Imported P&L report with {lines_created} lines for {from_date} to {to_date}")
    return {
        "success": True,
        "message": f"Successfully imported P&L report for {from_date} to {to_date}",
        "report": report,
        "lines_created": lines_created,
        "is_new": True,
    }


def import_and_export_trail_balance(tenant_id, user=None):
    """
    Import trail balance from Xero and export to files.
    Temporary function for testing.
    """
    from .exports import export_report_to_files
    
    # Import
    import_result = import_trail_balance_from_xero(tenant_id=tenant_id, user=user)
    report = import_result['report']
    
    # Export
    export_result = export_report_to_files(report.id)
    
    return {
        'success': True,
        'message': f"Imported and exported trail balance for {report.report_date}",
        'stats': {
            'trail_balance_export_files': export_result,
            'lines_imported': import_result.get('lines_created', 0)
        }
    }

