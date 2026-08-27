"""
Unit tests for Xero sync views and services (v3).
Tests data synchronization endpoints and services.
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
from django.utils import timezone
import datetime

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_sync.models import XeroTenantSchedule, XeroTaskExecutionLog, XeroLastUpdate
from apps.xero.xero_sync.services import update_xero_models

User = get_user_model()


class XeroUpdateModelsViewTest(TestCase):
    """Test XeroUpdateModelsView."""
    
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
        # XeroUpdateModelsView resolves an active credentials row before it will
        # sync anything (403 otherwise), so the happy path needs one.
        self.credentials = XeroClientCredentials.objects.create(
            user=self.user,
            client_id='test-client-id',
            client_secret='test-client-secret',
            scope=['accounting.transactions'],
            active=True,
        )

    # views.py does `from apps.xero.xero_sync.services import update_xero_models`,
    # so the view holds its own reference — patch the name the VIEW resolves.
    @patch('apps.xero.xero_sync.views.update_xero_models')
    def test_update_models_success(self, mock_update):
        """Test successful model update."""
        mock_update.return_value = {
            'success': True,
            'message': 'Updated successfully',
            'stats': {'accounts_updated': 10, 'duration_seconds': 5.0},
            'errors': []
        }
        
        response = self.client.post('/xero/sync/update/', {'tenant_id': 'test-tenant'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('message', response.data)
        self.assertIn('stats', response.data)
        mock_update.assert_called_once_with('test-tenant', user=self.user)
    
    @patch('apps.xero.xero_sync.views.update_xero_models')
    def test_update_models_with_errors(self, mock_update):
        """Test model update with errors."""
        mock_update.return_value = {
            'success': False,
            'message': 'Some errors occurred',
            'stats': {'duration_seconds': 5.0},
            'errors': ['Error 1', 'Error 2']
        }
        
        response = self.client.post('/xero/sync/update/', {'tenant_id': 'test-tenant'})
        self.assertEqual(response.status_code, status.HTTP_207_MULTI_STATUS)
        self.assertIn('errors', response.data)
    
    def test_update_models_no_tenant_id(self):
        """Test update without tenant_id."""
        response = self.client.post('/xero/sync/update/', {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    
    def test_update_models_tenant_not_found(self):
        """Test update with non-existent tenant."""
        response = self.client.post('/xero/sync/update/', {'tenant_id': 'non-existent'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class XeroTenantScheduleModelTest(TestCase):
    """Test XeroTenantSchedule model methods."""
    
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='test-tenant-123',
            tenant_name='Test Tenant'
        )
        self.schedule = XeroTenantSchedule.objects.create(
            tenant=self.tenant,
            enabled=True,
            update_interval_minutes=60
        )
    
    def test_schedule_str(self):
        """Test schedule string representation."""
        self.assertEqual(str(self.schedule), f"Schedule for {self.tenant.tenant_name}")
    
    def test_should_run_update_when_disabled(self):
        """Test should_run_update returns False when disabled."""
        self.schedule.enabled = False
        self.schedule.save()
        self.assertFalse(self.schedule.should_run_update())
    
    def test_should_run_update_when_no_next_run(self):
        """Test should_run_update returns True when next_run is None."""
        self.schedule.next_update_run = None
        self.schedule.save()
        self.assertTrue(self.schedule.should_run_update())
    
    def test_should_run_update_when_due(self):
        """Test should_run_update returns True when time is due."""
        past_time = timezone.now() - datetime.timedelta(minutes=5)
        self.schedule.next_update_run = past_time
        self.schedule.save()
        self.assertTrue(self.schedule.should_run_update())
    
    def test_should_run_update_when_not_due(self):
        """Test should_run_update returns False when time is not due."""
        future_time = timezone.now() + datetime.timedelta(hours=1)
        self.schedule.next_update_run = future_time
        self.schedule.save()
        self.assertFalse(self.schedule.should_run_update())


class XeroTaskExecutionLogModelTest(TestCase):
    """Test XeroTaskExecutionLog model methods."""
    
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='test-tenant-123',
            tenant_name='Test Tenant'
        )
        self.log = XeroTaskExecutionLog.objects.create(
            tenant=self.tenant,
            task_type='update_models',
            status='running'
        )
    
    def test_log_str(self):
        """Test log string representation."""
        expected = f"{self.tenant.tenant_name} - Update Models - running"
        self.assertEqual(str(self.log), expected)
    
    def test_mark_completed_with_duration(self):
        """Test mark_completed with explicit duration."""
        self.log.mark_completed(duration_seconds=120.5, records_processed=100, stats={'api_calls': 5})
        
        self.assertEqual(self.log.status, 'completed')
        self.assertIsNotNone(self.log.completed_at)
        self.assertEqual(self.log.duration_seconds, 120.5)
        self.assertEqual(self.log.records_processed, 100)
        self.assertEqual(self.log.stats, {'api_calls': 5})
    
    def test_mark_failed(self):
        """Test mark_failed sets error message."""
        error_msg = "Test error message"
        self.log.mark_failed(error_msg, duration_seconds=5.0)
        
        self.assertEqual(self.log.status, 'failed')
        self.assertIsNotNone(self.log.completed_at)
        self.assertEqual(self.log.error_message, error_msg)
        self.assertEqual(self.log.duration_seconds, 5.0)

