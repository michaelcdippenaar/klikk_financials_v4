"""
Unit tests for Xero core views (v3).
Tests tenant management endpoints.
"""
import sys
from unittest.mock import MagicMock
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
from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken

User = get_user_model()


class XeroTenantListViewTest(TestCase):
    """Test XeroTenantListView."""
    
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
        
        self.tenant1 = XeroTenant.objects.create(
            tenant_id='tenant-1',
            tenant_name='Tenant 1'
        )
        self.tenant2 = XeroTenant.objects.create(
            tenant_id='tenant-2',
            tenant_name='Tenant 2'
        )
        
        self.token1 = XeroTenantToken.objects.create(
            tenant=self.tenant1,
            credentials=self.credentials,
            token={'access_token': 'token1'},
            refresh_token='refresh1',
            expires_at=timezone.now() + datetime.timedelta(hours=1)
        )
        self.token2 = XeroTenantToken.objects.create(
            tenant=self.tenant2,
            credentials=self.credentials,
            token={'access_token': 'token2'},
            refresh_token='refresh2',
            expires_at=timezone.now() + datetime.timedelta(hours=1)
        )
    
    def test_list_tenants_success(self):
        """Test successful tenant listing."""
        response = self.client.get('/xero/core/tenants/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        
        tenant_ids = [t['tenant_id'] for t in response.data]
        self.assertIn('tenant-1', tenant_ids)
        self.assertIn('tenant-2', tenant_ids)
    
    def test_list_tenants_empty(self):
        """Test listing tenants when none exist."""
        XeroTenantToken.objects.all().delete()
        response = self.client.get('/xero/core/tenants/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)
    
    def test_list_tenants_unauthenticated(self):
        """Test listing tenants without authentication."""
        client = APIClient()
        response = client.get('/xero/core/tenants/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class XeroTenantModelTest(TestCase):
    """Test XeroTenant model."""
    
    def test_tenant_creation(self):
        """Test tenant creation."""
        tenant = XeroTenant.objects.create(
            tenant_id='test-tenant',
            tenant_name='Test Tenant'
        )
        self.assertEqual(str(tenant), 'Test Tenant')
        self.assertEqual(tenant.tenant_id, 'test-tenant')
    
    def test_tenant_unique_constraint(self):
        """Test tenant unique constraint."""
        XeroTenant.objects.create(
            tenant_id='test-tenant',
            tenant_name='Test Tenant'
        )
        with self.assertRaises(Exception):  # IntegrityError
            XeroTenant.objects.create(
                tenant_id='test-tenant',
                tenant_name='Another Tenant'
            )
