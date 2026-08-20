"""
Xero core views - tenant management.
"""
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroTenantToken
from apps.xero.xero_auth.credentials import resolve_active_credentials


class XeroTenantListView(APIView):
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request):
        """List all tenants connected to the user's credentials."""
        # Was `.get(user=request.user, active=True)` on the authenticated branch,
        # which raised DoesNotExist -> HTTP 500 for any logged-in user who did not
        # personally own a credentials row. The lockdown made every caller
        # authenticated, so that branch started carrying real traffic for the
        # first time and the console got 500s. See xero_auth/credentials.py.
        credentials = resolve_active_credentials(request)
        if credentials is None:
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
