"""
Unit tests for Xero cube services (v3).
Tests data processing service functions.
"""
import sys
from unittest.mock import MagicMock, patch
sys.modules['apscheduler'] = MagicMock()
sys.modules['apscheduler.schedulers'] = MagicMock()
sys.modules['apscheduler.schedulers.background'] = MagicMock()

from django.test import TestCase

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_cube.services import process_xero_data


class ProcessXeroDataServiceTest(TestCase):
    """Test process_xero_data service function."""
    
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='test-tenant-123',
            tenant_name='Test Tenant'
        )
    
    @patch('apps.xero.xero_cube.services.create_trail_balance')
    def test_process_xero_data_success(self, mock_create_trail_balance):
        """Test process_xero_data with successful execution."""
        mock_create_trail_balance.return_value = None
        
        result = process_xero_data(self.tenant.tenant_id)
        
        self.assertTrue(result['success'])
        self.assertIn('message', result)
        self.assertIn('stats', result)
        self.assertTrue(result['stats']['trail_balance_created'])
        # process_xero_data forwards the full rebuild contract, not just
        # `incremental`. Pin every argument so a silent default change here
        # (e.g. rebuild flipping to True) still fails the test.
        # A tenant with no previous build stamp derives a FULL scope, so the
        # journals were fully regenerated and the trail balance must be fully
        # rebuilt too (incremental=False) — never left to the date-window
        # fallback, which rebuilt one month of a full reprocess on 2026-09-03.
        mock_create_trail_balance.assert_called_once_with(
            self.tenant.tenant_id,
            incremental=False,
            rebuild=False,
            exclude_manual_journals=False,
            affected_periods=None,
        )
    
    def test_process_xero_data_tenant_not_found(self):
        """Test process_xero_data with non-existent tenant."""
        with self.assertRaises(ValueError) as context:
            process_xero_data('non-existent-tenant')
        
        self.assertIn('not found', str(context.exception).lower())

