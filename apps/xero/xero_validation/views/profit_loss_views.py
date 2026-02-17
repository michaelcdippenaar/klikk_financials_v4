"""
Profit & Loss Views

API views for Profit & Loss report operations including:
- Importing from Xero API
- Comparing with database values
- Exporting reports
"""
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..services import (
    compare_profit_loss,
    export_profit_loss_report_complete,
    import_profit_loss_from_xero,
)
from .common import (
    get_param,
    get_tenant_id,
    handle_validation_error,
    parse_date_string,
    parse_tolerance,
)


class ImportProfitLossView(APIView):
    """Import Profit and Loss report from Xero API. Supports both GET (query params) and POST (body)."""
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def get(self, request):
        """Allow GET with query params: tenant_id, from_date, to_date, periods, timeframe."""
        return self.post(request)

    def post(self, request):
        try:
            tenant_id = get_tenant_id(request)
            from_date_str = get_param(request, 'from_date')
            to_date_str = get_param(request, 'to_date')
            
            if not from_date_str or not to_date_str:
                return Response(
                    {"error": "from_date and to_date are required (YYYY-MM-DD format)"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            from_date = parse_date_string(from_date_str)
            to_date = parse_date_string(to_date_str)
            
            periods = int(get_param(request, 'periods', 11))  # 11 periods = 12 months
            timeframe = get_param(request, 'timeframe', 'MONTH')
            
            result = import_profit_loss_from_xero(
                tenant_id=tenant_id,
                from_date=from_date,
                to_date=to_date,
                periods=periods,
                timeframe=timeframe,
                user=request.user if request.user.is_authenticated else None
            )
            
            return Response({
                "message": result['message'],
                "report_id": result['report'].id,
                "from_date": result['report'].from_date,
                "to_date": result['report'].to_date,
                "periods": result['report'].periods,
                "lines_created": result.get('lines_created', 0),
                "is_new": result.get('is_new', False)
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to import P&L report")


class CompareProfitLossView(APIView):
    """Compare Xero Profit and Loss report with database trail balance (per month for 12-month period)."""
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def post(self, request):
        try:
            tenant_id = get_tenant_id(request)
            report_id = get_param(request, 'report_id')
            tolerance = parse_tolerance(get_param(request, 'tolerance'))
            
            result = compare_profit_loss(
                tenant_id=tenant_id,
                report_id=int(report_id) if report_id else None,
                tolerance=tolerance
            )
            
            return Response({
                "message": result['message'],
                "report_id": result['report_id'],
                "from_date": result['from_date'],
                "to_date": result['to_date'],
                "periods": result['periods'],
                "period_stats": result['period_stats'],
                "overall_statistics": result['overall_statistics']
            })
        except Exception as e:
            return handle_validation_error(e, "Failed to compare P&L report")


class ExportProfitLossCompleteView(APIView):
    """Export Profit and Loss report: both raw JSON and parsed lines to files."""
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def post(self, request):
        try:
            report_id = get_param(request, 'report_id')
            if not report_id:
                return Response({"error": "report_id is required"}, status=status.HTTP_400_BAD_REQUEST)
            
            result = export_profit_loss_report_complete(report_id=report_id)
            return Response({
                "success": True,
                "message": f"Exported P&L report: {result['lines_csv_file']['lines_exported']} lines",
                "report_id": result['report_id'],
                "from_date": result['from_date'],
                "to_date": result['to_date'],
                "periods": result['periods'],
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
            return handle_validation_error(e, "Failed to export P&L report")

