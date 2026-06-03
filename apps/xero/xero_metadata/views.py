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


# ============================================================================
# List endpoints for MCP / agent consumption (Phase 1.5 — 2026-06-03)
# ============================================================================

from django.db.models import Q  # noqa: E402
from apps.xero.xero_metadata.models import XeroContacts, XeroTracking  # noqa: E402


class XeroContactListView(APIView):
    """GET /xero/metadata/contacts/?tenant_id=&q=&is_supplier=&is_customer=&limit=&offset="""
    permission_classes = [AllowAny]

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        qs = XeroContacts.objects.filter(organisation_id=tenant_id)

        is_supplier = request.query_params.get('is_supplier')
        if is_supplier is not None:
            want = is_supplier.lower() in ('1', 'true', 'yes')
            qs = qs.filter(collection__IsSupplier=want)
        is_customer = request.query_params.get('is_customer')
        if is_customer is not None:
            want = is_customer.lower() in ('1', 'true', 'yes')
            qs = qs.filter(collection__IsCustomer=want)

        q_term = request.query_params.get('q')
        if q_term:
            qs = qs.filter(Q(name__icontains=q_term) | Q(contacts_id__icontains=q_term))

        total = qs.count()
        try:
            limit = min(int(request.query_params.get('limit', 100)), 1000)
        except ValueError:
            limit = 100
        try:
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except ValueError:
            offset = 0

        rows = qs.order_by('name')[offset:offset + limit]
        return Response({
            'count': total, 'limit': limit, 'offset': offset,
            'results': [{
                'contact_id': c.contacts_id, 'name': c.name,
                'is_supplier': bool((c.collection or {}).get('IsSupplier')),
                'is_customer': bool((c.collection or {}).get('IsCustomer')),
                'email': (c.collection or {}).get('EmailAddress', ''),
            } for c in rows],
        }, status=status.HTTP_200_OK)


class XeroTrackingListView(APIView):
    """GET /xero/metadata/tracking/?tenant_id=&active=&limit=&offset="""
    permission_classes = [AllowAny]

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        qs = XeroTracking.objects.filter(organisation_id=tenant_id)

        active = request.query_params.get('active')
        if active is not None:
            want = active.lower() in ('1', 'true', 'yes')
            qs = qs.filter(collection__Status='ACTIVE') if want else qs.exclude(collection__Status='ACTIVE')

        total = qs.count()
        try:
            limit = min(int(request.query_params.get('limit', 200)), 1000)
        except ValueError:
            limit = 200
        try:
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except ValueError:
            offset = 0

        rows = qs.order_by('name', 'option')[offset:offset + limit]
        return Response({
            'count': total, 'limit': limit, 'offset': offset,
            'results': [{
                'id': t.id, 'tracking_category_id': t.tracking_category_id,
                'category_name': t.name, 'option_name': t.option,
                'status': (t.collection or {}).get('Status', ''), 'category_slot': t.category_slot,
            } for t in rows],
        }, status=status.HTTP_200_OK)


class XeroAccountListView(APIView):
    """GET /xero/metadata/accounts/?tenant_id=&q=&type=&limit=&offset="""
    permission_classes = [AllowAny]

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        qs = XeroAccount.objects.filter(organisation_id=tenant_id)

        type_filter = request.query_params.get('type')
        if type_filter:
            qs = qs.filter(type__iexact=type_filter)
        q_term = request.query_params.get('q')
        if q_term:
            qs = qs.filter(Q(name__icontains=q_term) | Q(code__icontains=q_term))

        total = qs.count()
        try:
            limit = min(int(request.query_params.get('limit', 200)), 1000)
        except ValueError:
            limit = 200
        try:
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except ValueError:
            offset = 0

        rows = qs.order_by('code')[offset:offset + limit]
        return Response({
            'count': total, 'limit': limit, 'offset': offset,
            'results': [{
                'account_id': a.account_id, 'code': a.code, 'name': a.name,
                'type': a.type, 'tax_type': (a.collection or {}).get('TaxType', ''),
                'status': (a.collection or {}).get('Status', ''),
            } for a in rows],
        }, status=status.HTTP_200_OK)
