"""
Xero metadata views - account search and reference data endpoints.
"""
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_metadata.services import update_metadata
from apps.xero.xero_sync.api_call_logging import log_xero_api_calls


@login_required
def account_search(request):
    """Search for accounts by name."""
    tenant_id = request.GET.get('tenant_id')
    query = request.GET.get('q', '')
    if not tenant_id:
        return JsonResponse({'error': 'tenant_id is required'}, status=400)

    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        accounts = XeroAccount.objects.filter(
            organisation=tenant,
            name__icontains=query
        )[:20]
        results = [
            {'account_id': account.account_id, 'name': account.name, 'code': account.code}
            for account in accounts
        ]
        return JsonResponse(results, safe=False)
    except XeroTenant.DoesNotExist:
        return JsonResponse({'error': 'Tenant not found'}, status=404)


class XeroUpdateMetadataView(APIView):
    """
    API endpoint to trigger metadata updates (accounts, contacts, tracking categories).
    """
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
            
            # Trigger metadata update
            result = update_metadata(tenant_id, user=user)

            # Log API calls for rate limit tracking
            api_calls = result.get('stats', {}).get('api_calls', 0)
            log_xero_api_calls('metadata', api_calls, tenant=tenant)

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
            return Response({"error": f"Failed to update metadata: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
