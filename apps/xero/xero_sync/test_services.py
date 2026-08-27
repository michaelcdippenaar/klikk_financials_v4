"""
Unit tests for Xero sync services (v3).
Tests data synchronization service functions.
"""
import sys
from unittest.mock import MagicMock, patch
sys.modules['apscheduler'] = MagicMock()
sys.modules['apscheduler.schedulers'] = MagicMock()
sys.modules['apscheduler.schedulers.background'] = MagicMock()

from django.test import TestCase
from django.contrib.auth import get_user_model

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_sync.services import update_xero_models

User = get_user_model()


class UpdateXeroModelsServiceTest(TestCase):
    """Test update_xero_models service function."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.tenant = XeroTenant.objects.create(
            tenant_id='test-tenant-123',
            tenant_name='Test Tenant'
        )
        self.credentials = XeroClientCredentials.objects.create(
            user=self.user,
            client_id='test-client-id',
            client_secret='test-client-secret',
            scope=['accounting.transactions'],
            active=True
        )
    
    # update_xero_models has TWO outbound surfaces, and both must be stubbed or
    # the test reaches for a live Xero token:
    #   Group 1 (accounts / tracking / contacts) is delegated to
    #     apps.xero.xero_metadata.services.update_metadata, which builds its OWN
    #     XeroApiClient — patching xero_sync.services.XeroApiClient does not
    #     reach it. Left unpatched it raises "No token found for tenant ...",
    #     which update_xero_models swallows into errors[] -> success False.
    #   Group 2 (bank_transactions / invoices / payments / manual_journals) goes
    #     through xero_sync.services.XeroAccountingApi.
    @patch('apps.xero.xero_metadata.services.update_metadata')
    @patch('apps.xero.xero_sync.services.XeroApiClient')
    @patch('apps.xero.xero_sync.services.XeroAccountingApi')
    def test_update_xero_models_success(self, mock_api_class, mock_client_class, mock_update_metadata):
        """Test update_xero_models with successful execution."""
        mock_update_metadata.return_value = {
            'success': True,
            'errors': [],
            'stats': {
                'accounts_updated': 1,
                'tracking_categories_updated': 1,
                'contacts_updated': 1,
                'api_calls': 3,
            },
        }

        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Group 2 endpoints all answer without raising.
        for endpoint in ('bank_transactions', 'invoices', 'payments', 'manual_journals'):
            endpoint_mock = MagicMock()
            endpoint_mock.get.return_value = None
            getattr(mock_api, endpoint).return_value = endpoint_mock

        # Call the function
        result = update_xero_models(self.tenant.tenant_id, user=self.user)

        # Assertions
        self.assertEqual(result.get('errors', []), [])
        self.assertTrue(result['success'])
        self.assertIn('message', result)
        self.assertIn('stats', result)
        self.assertIn('duration_seconds', result['stats'])
        mock_update_metadata.assert_called_once_with(self.tenant.tenant_id, user=self.user)

    @patch('apps.xero.xero_metadata.services.update_metadata')
    @patch('apps.xero.xero_sync.services.XeroApiClient')
    @patch('apps.xero.xero_sync.services.XeroAccountingApi')
    def test_update_xero_models_with_errors(self, mock_api_class, mock_client_class, mock_update_metadata):
        """One failing endpoint must be collected, not raised, and flip success."""
        mock_update_metadata.return_value = {
            'success': True,
            'errors': [],
            'stats': {},
        }

        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        for endpoint in ('invoices', 'payments', 'manual_journals'):
            endpoint_mock = MagicMock()
            endpoint_mock.get.return_value = None
            getattr(mock_api, endpoint).return_value = endpoint_mock

        failing = MagicMock()
        failing.get.side_effect = Exception("API Error")
        mock_api.bank_transactions.return_value = failing

        # Call the function
        result = update_xero_models(self.tenant.tenant_id, user=self.user)

        # Should have errors but still return result
        self.assertFalse(result['success'])
        self.assertEqual(len(result.get('errors', [])), 1)
        self.assertIn('bank_transactions', result['errors'][0])


    def test_update_xero_models_tenant_not_found(self):
        """Test update_xero_models with non-existent tenant."""
        with self.assertRaises(ValueError) as context:
            update_xero_models('non-existent-tenant', user=self.user)
        
        self.assertIn('not found', str(context.exception).lower())


class XeroApiClientTest(TestCase):
    """Test XeroApiClient from xero_core."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
    
    def test_api_client_initialization(self):
        """Test XeroApiClient can be initialized."""
        from apps.xero.xero_core.services import XeroApiClient
        # Create credentials first
        credentials = XeroClientCredentials.objects.create(
            user=self.user,
            client_id='test-client-id',
            client_secret='test-client-secret',
            scope=['accounting.transactions'],
            active=True
        )
        client = XeroApiClient(self.user)
        self.assertIsNotNone(client)
        self.assertEqual(client.user, self.user)

