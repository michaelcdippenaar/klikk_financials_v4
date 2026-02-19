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
