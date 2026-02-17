"""
Xero authentication views.
"""
import base64
import datetime
import logging
import requests
from urllib.parse import urlencode
from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from xero_python.identity import IdentityApi
from xero_python.exceptions import AccountingBadRequestException as ApiException

from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken, XeroAuthSettings
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_core.services import XeroApiClient

logger = logging.getLogger(__name__)
User = get_user_model()

# Frontend URL to redirect to after OAuth callback
FRONTEND_URL = getattr(settings, 'FRONTEND_URL', 'http://localhost:9000')


class XeroAuthInitiateView(APIView):
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def get(self, request):
        """Initiate Xero OAuth2 flow by returning the authorization URL."""
        # TODO: When adding authentication back, filter by request.user
        # For now, get first active credentials (development only)
        try:
            if request.user.is_authenticated:
                credentials = XeroClientCredentials.objects.get(user=request.user, active=True)
            else:
                # For development: get first active credentials
                credentials = XeroClientCredentials.objects.filter(active=True).first()
                if not credentials:
                    return Response({"error": "No active Xero credentials found"}, status=status.HTTP_403_FORBIDDEN)
        except XeroClientCredentials.DoesNotExist:
            return Response({"error": "No active Xero credentials found"}, status=status.HTTP_403_FORBIDDEN)

        auth_settings = XeroAuthSettings.objects.first()
        if not auth_settings:
            return Response({"error": "Xero authentication settings not configured"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        scope = " ".join(credentials.scope) if credentials.scope else "accounting.transactions"
        redirect_uri = request.build_absolute_uri('/xero/callback/')
        auth_url = (
            f"{auth_settings.auth_url}?response_type=code"
            f"&client_id={credentials.client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&scope={scope}"
        )
        return Response({"auth_url": auth_url})


class XeroCallbackView(APIView):
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def _redirect_error(self, message):
        """Redirect to frontend with error message."""
        params = urlencode({'status': 'error', 'message': message})
        return HttpResponseRedirect(f"{FRONTEND_URL}/xero-connect?{params}")

    def get(self, request):
        # TODO: When adding authentication back, use request.user
        user_info = request.user.username if request.user.is_authenticated else "anonymous"
        logger.info(f"Processing callback for user {user_info}")
        code = request.query_params.get('code')
        if not code:
            logger.error("No code provided")
            return self._redirect_error("No authorization code provided")

        try:
            # TODO: When adding authentication back, filter by request.user
            # For now, get first active credentials (development only)
            if request.user.is_authenticated:
                credentials = XeroClientCredentials.objects.get(user=request.user, active=True)
            else:
                # For development: get first active credentials
                credentials = XeroClientCredentials.objects.filter(active=True).first()
                if not credentials:
                    logger.error("No active credentials found")
                    return self._redirect_error("No active Xero credentials found")
            logger.info(f"Credentials found: client_id={credentials.client_id}")
        except XeroClientCredentials.DoesNotExist:
            logger.error(f"No active credentials found")
            return self._redirect_error("No active Xero credentials found")

        auth_settings = XeroAuthSettings.objects.first()
        if not auth_settings:
            logger.error("XeroAuthSettings not configured")
            return self._redirect_error("Xero authentication settings not configured")

        # Exchange code for token
        token_url = auth_settings.access_token_url
        tokenb4 = f"{credentials.client_id}:{credentials.client_secret}"
        basic_token = base64.urlsafe_b64encode(tokenb4.encode()).decode()
        headers = {
            'Authorization': f"Basic {basic_token}",
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': request.build_absolute_uri('/xero/callback/'),
        }
        logger.info(f"Exchanging code: {code}")
        try:
            response = requests.post(token_url, headers=headers, data=data)
            response.raise_for_status()
            token_data = response.json()
            logger.info(f"Token data: {token_data}")
        except requests.RequestException as e:
            logger.error(f"Token exchange failed: {str(e)}")
            return self._redirect_error(f"Token exchange failed: {e}")

        if 'error' in token_data:
            logger.error(f"Token exchange error: {token_data['error']}")
            return self._redirect_error(token_data['error'])

        required_fields = ['access_token', 'expires_in']
        for field in required_fields:
            if field not in token_data:
                logger.error(f"Missing required field in token_data: {field}")
                return self._redirect_error(f"Invalid token data: missing {field}")

        # Configure ApiClient with temporary token
        # Note: We don't have a tenant_id yet, so we create a temporary token object
        # and manually set it on the api_client
        # Create a minimal tenant object for the temp token (won't be saved to DB)
        temp_tenant = XeroTenant(tenant_id='temp', tenant_name='Temp')
        temp_tenant_token = XeroTenantToken(
            tenant=temp_tenant,
            credentials=credentials,
            token=token_data,
            refresh_token=token_data.get('refresh_token'),
            expires_at=timezone.now() + datetime.timedelta(seconds=token_data.get('expires_in'))
        )
        
        # TODO: When adding authentication back, use request.user
        # For now, use credentials.user (development only)
        api_client = XeroApiClient(credentials.user)
        # Set tenant_token BEFORE configure_api_client so the token getter can find it
        api_client.tenant_token = temp_tenant_token
        api_client.configure_api_client(temp_tenant_token)
        identity_api = IdentityApi(api_client.api_client)

        # Fetch all tenant connections
        try:
            logger.info("Calling get_connections")
            connections = identity_api.get_connections()
            logger.info(f"Connections retrieved: {len(connections)} tenants")
        except ApiException as e:
            logger.error(f"ApiException in get_connections: {str(e)}")
            return self._redirect_error(f"Failed to fetch connections: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in get_connections: {str(e)}")
            return self._redirect_error(f"Unexpected error: {e}")

        if not connections:
            logger.warning("No tenant connections found")
            return self._redirect_error("No tenant connections found")

        # Process all tenants
        created_tenants = []
        for connection in connections:
            tenant_id = connection.tenant_id
            tenant_name = connection.tenant_name if connection.tenant_name else 'Unnamed Tenant'
            logger.info(f"Processing tenant ID: {tenant_id}, Name: {tenant_name}")

            # Create or update XeroTenant
            tenant, _ = XeroTenant.objects.get_or_create(
                tenant_id=tenant_id,
                defaults={'tenant_name': tenant_name}
            )

            # Save token to JSONField (new approach)
            expires_at = timezone.now() + datetime.timedelta(seconds=token_data.get('expires_in'))
            credentials.set_tenant_token_data(
                tenant_id=tenant_id,
                token_data=token_data,
                refresh_token=token_data.get('refresh_token'),
                expires_at=expires_at,
                connected_at=timezone.now()
            )
            
            # Also create/update XeroTenantToken model for backward compatibility
            XeroTenantToken.objects.update_or_create(
                tenant=tenant,
                credentials=credentials,
                defaults={
                    'token': token_data,
                    'refresh_token': token_data.get('refresh_token'),
                    'expires_at': expires_at
                }
            )
            created_tenants.append(tenant_id)

        logger.info(f"Stored tokens for tenants: {created_tenants}")

        # Redirect to frontend with success params
        tenant_names = [
            XeroTenant.objects.get(tenant_id=tid).tenant_name
            for tid in created_tenants
        ]
        params = urlencode({
            'status': 'success',
            'tenants': ','.join(tenant_names),
            'count': len(created_tenants),
        })
        return HttpResponseRedirect(f"{FRONTEND_URL}/xero-connect?{params}")


class XeroConnectionStatusView(APIView):
    """Return the current Xero connection status for all tenants."""
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def get(self, request):
        try:
            if request.user.is_authenticated:
                credentials = XeroClientCredentials.objects.filter(user=request.user, active=True).first()
            else:
                credentials = XeroClientCredentials.objects.filter(active=True).first()
        except Exception:
            credentials = None

        if not credentials:
            return Response({
                'connected': False,
                'has_credentials': False,
                'tenants': [],
                'message': 'No active Xero credentials configured',
            })

        # Check which tenants have valid tokens
        tenant_tokens = credentials.tenant_tokens or {}
        connected_tenants = []

        for tenant_id, token_info in tenant_tokens.items():
            try:
                tenant = XeroTenant.objects.get(tenant_id=tenant_id)
                expires_at = token_info.get('expires_at', '')
                is_expired = False
                if expires_at:
                    try:
                        from django.utils.dateparse import parse_datetime
                        exp_dt = parse_datetime(expires_at)
                        if exp_dt and exp_dt < timezone.now():
                            is_expired = True
                    except (ValueError, TypeError):
                        pass

                connected_at = token_info.get('connected_at', '')
                connected_tenants.append({
                    'tenant_id': tenant_id,
                    'tenant_name': tenant.tenant_name,
                    'connected_at': connected_at,
                    'token_expired': is_expired,
                    'has_refresh_token': bool(token_info.get('refresh_token')),
                })
            except XeroTenant.DoesNotExist:
                continue

        return Response({
            'connected': len(connected_tenants) > 0,
            'has_credentials': True,
            'tenants': connected_tenants,
            'credentials_client_id': credentials.client_id[:8] + '...' if credentials.client_id else None,
        })


class XeroCredentialsView(APIView):
    """Save or update Xero API credentials (client_id, client_secret)."""
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    # Default Xero OAuth2 URLs
    XERO_AUTH_URL = 'https://login.xero.com/identity/connect/authorize'
    XERO_TOKEN_URL = 'https://identity.xero.com/connect/token'

    # Default scopes for full accounting access
    DEFAULT_SCOPES = [
        'openid', 'profile', 'email', 'offline_access',
        'accounting.transactions', 'accounting.transactions.read',
        'accounting.reports.read', 'accounting.journals.read',
        'accounting.settings', 'accounting.settings.read',
        'accounting.contacts', 'accounting.contacts.read',
        'accounting.attachments', 'accounting.attachments.read',
    ]

    def post(self, request):
        client_id = request.data.get('client_id', '').strip()
        client_secret = request.data.get('client_secret', '').strip()

        if not client_id or not client_secret:
            return Response(
                {'error': 'Both client_id and client_secret are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get or create a user to associate the credentials with
        if request.user.is_authenticated:
            user = request.user
        else:
            # Dev mode: use or create a default admin user
            user = User.objects.filter(is_superuser=True).first()
            if not user:
                return Response(
                    {'error': 'No admin user found. Please log in first.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Create or update credentials
        credentials, created = XeroClientCredentials.objects.update_or_create(
            user=user,
            active=True,
            defaults={
                'client_id': client_id,
                'client_secret': client_secret,
                'scope': self.DEFAULT_SCOPES,
            },
        )

        # Ensure XeroAuthSettings exists with Xero's OAuth URLs
        XeroAuthSettings.objects.get_or_create(
            defaults={
                'auth_url': self.XERO_AUTH_URL,
                'access_token_url': self.XERO_TOKEN_URL,
                'refresh_url': self.XERO_TOKEN_URL,
            },
        )

        return Response({
            'success': True,
            'created': created,
            'client_id': client_id[:8] + '...',
            'message': 'Credentials saved. You can now connect to Xero.',
        })
