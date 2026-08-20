"""
Xero core views - tenant management.
"""
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken


class XeroTenantListView(APIView):
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request):
        """List all tenants connected to the user's credentials."""
        # TODO: When adding authentication back, filter by request.user
        # For now, get first active credentials (development only)
        if request.user.is_authenticated:
            credentials = XeroClientCredentials.objects.get(user=request.user, active=True)
        else:
            credentials = XeroClientCredentials.objects.filter(active=True).first()
            if not credentials:
                return Response({"error": "No active Xero credentials found"}, status=status.HTTP_403_FORBIDDEN)
        tenant_tokens = XeroTenantToken.objects.filter(credentials=credentials)
        tenants = [
            {
                'tenant_id': token.tenant.tenant_id,
                'tenant_name': token.tenant.tenant_name,
                'connected_at': token.connected_at,
                'expires_at': token.expires_at
            }
            for token in tenant_tokens
        ]
        return Response(tenants)
