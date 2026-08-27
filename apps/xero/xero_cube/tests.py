"""
Unit tests for Xero cube views (v3).
Tests data processing and summary endpoints.
"""
import sys
from unittest.mock import MagicMock, patch
sys.modules['apscheduler'] = MagicMock()
sys.modules['apscheduler.schedulers'] = MagicMock()
sys.modules['apscheduler.schedulers.background'] = MagicMock()

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_data.models import XeroJournals
from apps.xero.xero_cube.models import XeroTrailBalance, XeroBalanceSheet
from apps.xero.xero_cube.services import process_xero_data

User = get_user_model()


class XeroProcessDataViewTest(TestCase):
    """Test XeroProcessDataView."""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)
        
        self.tenant = XeroTenant.objects.create(
            tenant_id='test-tenant',
            tenant_name='Test Tenant'
        )
    
    # Patch the name the VIEW resolves. views.py does
    # `from apps.xero.xero_cube.services import process_xero_data`, so the view
    # holds its own reference and patching the services module is a no-op that
    # would silently run the real pipeline.
    @patch('apps.xero.xero_cube.views.process_xero_data')
    def test_process_data_success(self, mock_process):
        """Test successful data processing."""
        mock_process.return_value = {
            'success': True,
            'message': 'Processed successfully',
            'stats': {'trail_balance_created': True, 'duration_seconds': 10.0}
        }
        
        response = self.client.post('/xero/cube/process/', {'tenant_id': 'test-tenant'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('message', response.data)
        self.assertIn('stats', response.data)
        mock_process.assert_called_once_with(
            'test-tenant',
            rebuild_trail_balance=False,
            exclude_manual_journals=False,
            calculate_pnl_ytd=True,
        )
    
    def test_process_data_no_tenant_id(self):
        """Test process without tenant_id."""
        response = self.client.post('/xero/cube/process/', {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class XeroDataSummaryViewTest(TestCase):
    """Test XeroDataSummaryView."""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)
        
        self.tenant = XeroTenant.objects.create(
            tenant_id='test-tenant',
            tenant_name='Test Tenant'
        )
    
    def test_summary_success(self):
        """Test successful summary retrieval."""
        response = self.client.get('/xero/cube/summary/', {'tenant_id': 'test-tenant'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['tenant_id'], 'test-tenant')
        self.assertEqual(response.data['tenant_name'], 'Test Tenant')
        self.assertIn('accounts_count', response.data)
        self.assertIn('journals_count', response.data)
        self.assertIn('trail_balance_count', response.data)
        self.assertIn('balance_sheet_count', response.data)
    
    def test_summary_no_tenant_id(self):
        """Test summary without tenant_id."""
        response = self.client.get('/xero/cube/summary/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    
    def test_summary_tenant_not_found(self):
        """Test summary with non-existent tenant."""
        response = self.client.get('/xero/cube/summary/', {'tenant_id': 'non-existent'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
