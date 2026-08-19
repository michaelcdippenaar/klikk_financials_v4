"""
Xero sync views - data synchronization endpoints.
"""
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_sync.api_call_logging import get_api_call_stats
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_sync.services import update_xero_models


class XeroUpdateModelsView(APIView):
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def post(self, request):
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            # TODO: When adding authentication back, use request.user
            # For now, get user from first active credentials (development only)
            if request.user.is_authenticated:
                user = request.user
            else:
                credentials = XeroClientCredentials.objects.filter(active=True).first()
                if not credentials:
                    return Response({"error": "No active Xero credentials found"}, status=status.HTTP_403_FORBIDDEN)
                user = credentials.user
            # Use the service function for consistency with scheduled tasks
            result = update_xero_models(tenant_id, user=user)
            
            if result['success']:
                return Response({
                    "message": result['message'],
                    "stats": result['stats']
                })
            else:
                return Response({
                    "message": result['message'],
                    "errors": result['errors'],
                    "stats": result['stats']
                }, status=status.HTTP_207_MULTI_STATUS)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({"error": f"Failed to update data: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class XeroApiCallStatsView(APIView):
    """
    Get Xero API call statistics for Admin Console display.
    Query params: tenant_id (optional) - filter by tenant
    """
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        stats = get_api_call_stats(tenant_id=tenant_id)
        return Response(stats)


class XeroProcessStatusView(APIView):
    """
    Real per-process last-success status for the pipeline / processes page.

    The Processes page previously showed only this browser session's in-memory
    activity (localStorage), so it looked stale — it never reflected the syncs
    that actually ran (daily pipeline, scheduled tasks, other machines).

    This endpoint reads the authoritative timestamps that every sync run writes:
      - XeroLastUpdate.date  (per Xero end_point, per tenant)
      - XeroDocument.updated_at  (document import table — no XeroLastUpdate endpoint)

    so the page reflects reality on load.

    Query params: tenant_id (optional) — filter by tenant.

    Response:
        {
          "stages": {
            "<stage-id>": {
              "state": "succeeded" | "idle",
              "last_success_at": "<iso8601>" | null
            }, ...
          }
        }
    """
    permission_classes = [AllowAny]  # matches sibling views in this module

    # Stage id (matches the frontend PipelineStatusStrip / KOperationCard ids)
    # -> XeroLastUpdate end_point(s). The latest date across the group wins.
    STAGE_ENDPOINTS = {
        'metadata':      ['accounts', 'contacts', 'tracking_categories'],
        'data':          ['bank_transactions', 'payments', 'credit_notes',
                          'prepayments', 'overpayments', 'bank_transfers',
                          'manual_journals', 'journals'],
        'invoices':      ['invoices'],
        'journals':      ['journals'],
        'trail-balance': ['trail_balance'],
    }

    def get(self, request):
        from django.db.models import Max
        from apps.xero.xero_sync.models import XeroLastUpdate
        from apps.xero.xero_data.models import XeroDocument

        tenant_id = request.query_params.get('tenant_id')

        lu_qs = XeroLastUpdate.objects.all()
        if tenant_id:
            lu_qs = lu_qs.filter(organisation_id=tenant_id)

        # One query: latest date per end_point.
        latest_by_endpoint = {
            row['end_point']: row['last']
            for row in lu_qs.values('end_point').annotate(last=Max('date'))
        }

        stages = {}
        for stage_id, endpoints in self.STAGE_ENDPOINTS.items():
            dates = [latest_by_endpoint.get(ep) for ep in endpoints]
            dates = [d for d in dates if d is not None]
            last = max(dates) if dates else None
            stages[stage_id] = {
                'state': 'succeeded' if last else 'idle',
                'last_success_at': last.isoformat() if last else None,
            }

        # Documents have no XeroLastUpdate end_point — derive from the import table.
        doc_qs = XeroDocument.objects.all()
        if tenant_id:
            doc_qs = doc_qs.filter(organisation_id=tenant_id)
        doc_last = doc_qs.aggregate(last=Max('updated_at'))['last']
        stages['documents'] = {
            'state': 'succeeded' if doc_last else 'idle',
            'last_success_at': doc_last.isoformat() if doc_last else None,
        }

        return Response({'stages': stages})
