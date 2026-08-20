"""
Trial Balance Views

API views for Trial Balance report operations including:
- Importing from Xero API
- Comparing with database values
- Validating balance sheet accounts
- Exporting reports
- Adding income statement entries
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.xero.xero_core.models import XeroTenant
from ..models import TrailBalanceComparison, XeroTrailBalanceReport
from ..services import (
    add_income_statement_to_trail_balance_report,
    compare_trail_balance,
    export_all_line_items_to_csv,
    export_trail_balance_report_complete,
    import_and_export_trail_balance,
    import_trail_balance_from_xero,
    validate_balance_sheet_accounts,
    validate_balance_sheet_complete,
)
from .common import (
    DEFAULT_TARGET_ACCOUNT_CODE,
    get_param,
    get_tenant_id,
    handle_validation_error,
    parse_date_string,
    parse_tolerance,
)


class ValidateBalanceSheetCompleteView(APIView):
    """
    Combined validation endpoint that can run all steps or individual steps.
    Use flags to control which steps to execute:
    - import_trail_balance_only: Only import trail balance
    - compare_only: Only compare trail balance (requires existing report)
    - validate_only: Only validate balance sheet (requires existing report)
    - export_line_items: Export line items to CSV
    - add_income_statement: Add income statement to report
    """
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            tenant_id = get_tenant_id(request)
            report_date_str = get_param(request, 'report_date')
            report_date = parse_date_string(report_date_str) if report_date_str else None
            tolerance = parse_tolerance(get_param(request, 'tolerance'))
            
            # Get flags for step control
            import_trail_balance_only = get_param(request, 'import_trail_balance_only', 'false').lower() == 'true'
            compare_only = get_param(request, 'compare_only', 'false').lower() == 'true'
            validate_only = get_param(request, 'validate_only', 'false').lower() == 'true'
            export_line_items = get_param(request, 'export_line_items', 'false').lower() == 'true'
            add_income_statement = get_param(request, 'add_income_statement', 'false').lower() == 'true'
            target_account_code = get_param(request, 'target_account_code', DEFAULT_TARGET_ACCOUNT_CODE)
        
            result = validate_balance_sheet_complete(
                tenant_id=tenant_id,
                report_date=report_date,
                user=request.user if request.user.is_authenticated else None,
                tolerance=tolerance,
                import_trail_balance_only=import_trail_balance_only,
                compare_only=compare_only,
                validate_only=validate_only,
                export_line_items=export_line_items,
                add_income_statement=add_income_statement,
                target_account_code=target_account_code
            )

            stats = result.get('stats', {})
            validate_stats = stats.get('validate', {})
            validate_statistics = validate_stats.get('statistics', {})

            return Response({
                "success": result.get('success', True),
                "message": result.get('message', ''),
                "overall_status": result.get('overall_status', 'unknown'),
                "report_id": result.get('report_id'),
                "report_date": result.get('report_date'),
                "steps_executed": result.get('steps_executed', []),
                "stats": stats,
                "missing_accounts": validate_statistics.get('missing_accounts', []),
                "missing_transactions": validate_statistics.get('missing_transactions', []),
                "amount_mismatches": validate_statistics.get('amount_mismatches', []),
            })
        except Exception as e:
            return handle_validation_error(e, "Error in combined validation")


class ImportTrailBalanceView(APIView):
    """Import trail balance report from Xero API."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            tenant_id = get_tenant_id(request)
            report_date_str = get_param(request, 'report_date')
            report_date = parse_date_string(report_date_str) if report_date_str else None
            
            result = import_trail_balance_from_xero(
                tenant_id=tenant_id,
                report_date=report_date,
                user=request.user if request.user.is_authenticated else None
            )
            
            return Response({
                "message": result['message'],
                "report_id": result['report'].id,
                "report_date": result['report'].report_date,
                "lines_created": result.get('lines_created', 0),
                "is_new": result.get('is_new', False)
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to import trail balance")


class CompareTrailBalanceView(APIView):
    """Compare Xero trail balance report with database trail balance."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            tenant_id = get_tenant_id(request)
            report_id = get_param(request, 'report_id')
            report_date_str = get_param(request, 'report_date')
            report_date = parse_date_string(report_date_str) if report_date_str else None
            tolerance = parse_tolerance(get_param(request, 'tolerance'))
            
            result = compare_trail_balance(
                tenant_id=tenant_id,
                report_id=int(report_id) if report_id else None,
                report_date=report_date,
                tolerance=tolerance
            )
            
            return Response({
                "message": result['message'],
                "report_id": result['report_id'],
                "report_date": result['report_date'],
                "statistics": result['statistics']
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to compare trail balance")


class TrailBalanceComparisonDetailsView(APIView):
    """Get detailed comparison results for a specific report."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request, report_id):
        try:
            # Get tenant_id from request to filter by tenant
            tenant_id = get_param(request, 'tenant_id')
            if tenant_id:
                try:
                    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
                    report = XeroTrailBalanceReport.objects.get(id=report_id, organisation=organisation)
                except XeroTenant.DoesNotExist:
                    return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)
            else:
                # Fallback: get report without tenant filter (for backward compatibility)
                report = XeroTrailBalanceReport.objects.get(id=report_id)
            
            from django.db.models import F
            comparisons = TrailBalanceComparison.objects.filter(
                report=report
            ).select_related('account').order_by(
                F('difference').desc(nulls_last=True), 'account__code'
            )
            
            comparison_data = [{
                'account_code': comp.account.code,
                'account_name': comp.account.name,
                'xero_value': str(comp.xero_value),
                'db_value': str(comp.db_value),
                'difference': str(comp.difference),
                'match_status': comp.match_status,
                'notes': comp.notes
            } for comp in comparisons]
            
            return Response({
                "report_id": report.id,
                "report_date": report.report_date,
                "organisation": report.organisation.tenant_name,
                "comparisons": comparison_data,
                "total_comparisons": len(comparison_data)
            })
        except Exception as e:
            return handle_validation_error(e)


class ImportAndExportTrailBalanceView(APIView):
    """Temporary view to import trail balance report and export to files (testing purposes only)."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            tenant_id = get_tenant_id(request)
            result = import_and_export_trail_balance(
                tenant_id=tenant_id,
                user=request.user if request.user.is_authenticated else None
            )
            
            return Response({
                "success": result.get('success', False),
                "message": result.get('message', ''),
                "stats": result.get('stats', {}),
                "export_files": result.get('stats', {}).get('trail_balance_export_files', {})
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to import/export trail balance")


class ValidateBalanceSheetAccountsView(APIView):
    """Validate balance sheet accounts from Xero trail balance report against database (cumulative YTD)."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            tenant_id = get_tenant_id(request)
            report_id = get_param(request, 'report_id')
            report_date_str = get_param(request, 'report_date')
            report_date = parse_date_string(report_date_str) if report_date_str else None
            tolerance = parse_tolerance(get_param(request, 'tolerance'))
            
            result = validate_balance_sheet_accounts(
                tenant_id=tenant_id,
                report_id=int(report_id) if report_id else None,
                report_date=report_date,
                tolerance=tolerance
            )
            
            return Response({
                "success": result.get('success', False),
                "overall_status": result.get('overall_status', 'unknown'),
                "message": result.get('message', ''),
                "report_id": result.get('report_id'),
                "report_date": result.get('report_date'),
                "statistics": result.get('statistics', {}),
                "validations": result.get('validations', [])
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to validate balance sheet accounts")


class ExportLineItemsView(APIView):
    """Export all line items from a trial balance report to CSV."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            report_id = get_param(request, 'report_id')
            if not report_id:
                return Response({"error": "report_id is required"}, status=status.HTTP_400_BAD_REQUEST)
            
            result = export_all_line_items_to_csv(report_id=report_id)
            return Response({
                "success": True,
                "message": f"Exported {result['lines_exported']} line items",
                "file_path": result['file_path'],
                "filename": result['filename'],
                "lines_exported": result['lines_exported']
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to export line items")


class ExportTrailBalanceCompleteView(APIView):
    """Export Trail Balance report: both raw JSON and parsed lines to files."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            report_id = get_param(request, 'report_id')
            if not report_id:
                return Response({"error": "report_id is required"}, status=status.HTTP_400_BAD_REQUEST)
            
            result = export_trail_balance_report_complete(report_id=report_id)
            return Response({
                "success": True,
                "message": f"Exported Trail Balance report: {result['lines_csv_file']['lines_exported']} lines",
                "report_id": result['report_id'],
                "report_date": result['report_date'],
                "files_saved_to": result['export_dir'],
                "raw_json_file": {
                    "filename": result['raw_json_file']['filename'],
                    "file_path": result['raw_json_file']['file_path']
                },
                "lines_csv_file": {
                    "filename": result['lines_csv_file']['filename'],
                    "file_path": result['lines_csv_file']['file_path'],
                    "lines_exported": result['lines_csv_file']['lines_exported']
                }
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to export Trail Balance report")


class AddIncomeStatementToReportView(APIView):
    """Add income statement (P&L) entries to a trial balance report (uses latest report if report_id not provided)."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def post(self, request):
        try:
            report_id = get_param(request, 'report_id')
            tenant_id = get_param(request, 'tenant_id')
            
            if not report_id and not tenant_id:
                return Response(
                    {"error": "Either report_id or tenant_id is required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            target_account_code = get_param(request, 'target_account_code', DEFAULT_TARGET_ACCOUNT_CODE)
            result = add_income_statement_to_trail_balance_report(
                report_id=report_id,
                tenant_id=tenant_id,
                target_account_code=target_account_code
            )
            
            return Response({
                "success": True,
                "message": result.get('message', 'Income statement entry added successfully'),
                "lines_created": result.get('lines_created', 0),
                "pnl_value": result.get('pnl_value'),
                "revenue_total": result.get('revenue_total'),
                "expense_total": result.get('expense_total'),
                "report_id": result.get('report_id'),
                "report_date": str(result.get('report_date')) if result.get('report_date') else None
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to add income statement")

