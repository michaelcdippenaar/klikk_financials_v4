"""
Unit tests for Xero authentication views (v3).
Tests OAuth flow initiation and callback handling.
"""
import sys
from unittest.mock import MagicMock, patch, Mock
sys.modules['apscheduler'] = MagicMock()
sys.modules['apscheduler.schedulers'] = MagicMock()
sys.modules['apscheduler.schedulers.background'] = MagicMock()

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from django.utils import timezone
import datetime
from urllib.parse import parse_qs, urlparse

from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken, XeroAuthSettings
from apps.xero.xero_core.models import XeroTenant

User = get_user_model()


def _redirect_params(response):
    """Query params of a Location header, as {name: [values]}."""
    return parse_qs(urlparse(response['Location']).query)


class XeroAuthInitiateViewTest(TestCase):
    """Test XeroAuthInitiateView."""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)
        
        self.credentials = XeroClientCredentials.objects.create(
            user=self.user,
            client_id='test-client-id',
            client_secret='test-client-secret',
            scope=['accounting.transactions'],
            active=True
        )
        
        self.auth_settings = XeroAuthSettings.objects.create(
            auth_url='https://login.xero.com/identity/connect/authorize',
            access_token_url='https://identity.xero.com/connect/token',
            refresh_url='https://identity.xero.com/connect/token'
        )
    
    def test_auth_initiate_success(self):
        """Test successful auth initiation."""
        response = self.client.get('/xero/auth/initiate/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('auth_url', response.data)
        self.assertIn('test-client-id', response.data['auth_url'])
        self.assertIn('accounting.transactions', response.data['auth_url'])
    
    def test_auth_initiate_no_credentials(self):
        """Test auth initiation without credentials."""
        self.credentials.delete()
        response = self.client.get('/xero/auth/initiate/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('error', response.data)
    
    def test_auth_initiate_no_settings(self):
        """Test auth initiation without auth settings."""
        self.auth_settings.delete()
        response = self.client.get('/xero/auth/initiate/')
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn('error', response.data)
    
    def test_auth_initiate_unauthenticated(self):
        """Test auth initiation without authentication."""
        client = APIClient()
        response = client.get('/xero/auth/initiate/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class XeroCallbackViewTest(TestCase):
    """Test XeroCallbackView."""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)
        
        self.credentials = XeroClientCredentials.objects.create(
            user=self.user,
            client_id='test-client-id',
            client_secret='test-client-secret',
            scope=['accounting.transactions'],
            active=True
        )
        
        self.auth_settings = XeroAuthSettings.objects.create(
            auth_url='https://login.xero.com/identity/connect/authorize',
            access_token_url='https://identity.xero.com/connect/token',
            refresh_url='https://identity.xero.com/connect/token'
        )
    
    @patch('apps.xero.xero_auth.views.requests.post')
    @patch('apps.xero.xero_auth.views.IdentityApi')
    @patch('apps.xero.xero_auth.views.XeroApiClient')
    def test_callback_success(self, mock_api_client, mock_identity_api, mock_post):
        """Test successful OAuth callback."""
        # Mock token exchange
        mock_response = Mock()
        mock_response.json.return_value = {
            'access_token': 'test-token',
            'refresh_token': 'test-refresh',
            'expires_in': 3600
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response
        
        # Mock identity API
        mock_identity_instance = Mock()
        mock_connection = Mock()
        mock_connection.tenant_id = 'test-tenant-123'
        mock_connection.tenant_name = 'Test Tenant'
        mock_identity_instance.get_connections.return_value = [mock_connection]
        mock_identity_api.return_value = mock_identity_instance
        
        # Mock API client
        mock_client_instance = Mock()
        mock_api_client.return_value = mock_client_instance
        mock_client_instance.api_client = Mock()
        
        response = self.client.get('/xero/callback/', {'code': 'test-code'})

        # XeroCallbackView is the OAuth redirect target for Xero, which lands a
        # BROWSER here — so its contract is a 302 back to the frontend carrying
        # the outcome in the query string, not a JSON body. See the comment on
        # XeroCallbackView.
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        params = _redirect_params(response)
        self.assertEqual(params['status'], ['success'])
        self.assertEqual(params['tenants'], ['Test Tenant'])
        self.assertEqual(params['count'], ['1'])

        # Verify tenant was created
        tenant = XeroTenant.objects.get(tenant_id='test-tenant-123')
        self.assertEqual(tenant.tenant_name, 'Test Tenant')
        
        # Verify token was created
        token = XeroTenantToken.objects.get(tenant=tenant, credentials=self.credentials)
        self.assertIsNotNone(token)
    
    def test_callback_no_code(self):
        """A callback with no ?code= must bounce to the frontend error page."""
        response = self.client.get('/xero/callback/')
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        params = _redirect_params(response)
        self.assertEqual(params['status'], ['error'])
        self.assertEqual(params['message'], ['No authorization code provided'])

    def test_callback_no_credentials(self):
        """With no active credentials the callback must not attempt a token exchange."""
        self.credentials.delete()
        response = self.client.get('/xero/callback/', {'code': 'test-code'})
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        params = _redirect_params(response)
        self.assertEqual(params['status'], ['error'])
        self.assertEqual(params['message'], ['No active Xero credentials found'])
        # Nothing was persisted from an unauthenticatable callback.
        self.assertFalse(XeroTenantToken.objects.exists())
