"""
Reconciliation view: single process to get P&L and Balance Sheet, compare to trail balance, per financial year.
"""
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_sync.api_call_logging import log_xero_api_calls
from ..services import reconcile_reports_for_financial_year
from .common import get_param, get_tenant_id, handle_validation_error, parse_tolerance


class ReconcileReportsView(APIView):
    """
    Get Profit & Loss and Balance Sheet from Xero, compare to our trail balance, return report per financial year.

    Query/body params:
        tenant_id: required
        financial_year: required (e.g. 2024 for FY July 2024 – June 2025 if fiscal start is July)
        fiscal_year_start_month: optional, uses tenant's value from Xero Organisation when not provided
        tolerance: optional, default 0.01
    """
    permission_classes = [AllowAny]  # TODO: IsAuthenticated for production

    def get(self, request):
        return self._run(request)

    def post(self, request):
        return self._run(request)

    def _run(self, request):
        try:
            tenant_id = get_tenant_id(request)
            financial_year_str = get_param(request, "financial_year")
            if not financial_year_str:
                return Response(
                    {"error": "financial_year is required (e.g. 2024)"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                financial_year = int(financial_year_str)
            except (ValueError, TypeError):
                return Response(
                    {"error": "financial_year must be an integer (e.g. 2024)"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            fiscal_year_start_month = get_param(request, "fiscal_year_start_month")
            if fiscal_year_start_month is not None:
                try:
                    fiscal_year_start_month = int(fiscal_year_start_month)
                except (ValueError, TypeError):
                    fiscal_year_start_month = None  # Use tenant's value from Xero
            # else: None - reconciliation will use tenant's fiscal year from Xero Organisation
            tolerance = parse_tolerance(get_param(request, "tolerance"))

            result = reconcile_reports_for_financial_year(
                tenant_id=tenant_id,
                financial_year=financial_year,
                fiscal_year_start_month=fiscal_year_start_month,
                tolerance=tolerance,
                user=request.user if request.user.is_authenticated else None,
            )
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
            api_calls = result.get('api_calls', 0)
            log_xero_api_calls('reconcile', api_calls, tenant=tenant)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return handle_validation_error(e, "Reconciliation failed")
