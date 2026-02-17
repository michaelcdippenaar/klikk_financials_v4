"""
Xero Webhooks Models

This module provides models for managing Xero webhook subscriptions
and tracking incoming webhook events.

Xero webhook documentation: https://developer.xero.com/documentation/guides/webhooks/overview
"""
from django.db import models
from django.utils import timezone
from apps.xero.xero_core.models import XeroTenant


class WebhookSubscription(models.Model):
    """
    Tracks webhook subscriptions for Xero tenants.
    
    Each subscription is associated with a tenant and a webhook key
    used for signature validation.
    """
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('failed', 'Failed'),
    ]
    
    tenant = models.ForeignKey(
        XeroTenant, 
        on_delete=models.CASCADE, 
        related_name='webhook_subscriptions'
    )
    webhook_key = models.CharField(
        max_length=255,
        help_text="Secret key used for webhook signature validation"
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='pending'
    )
    
    # Subscription configuration
    event_types = models.JSONField(
        default=list,
        blank=True,
        help_text="List of event types to subscribe to (e.g., ['INVOICE', 'CONTACT'])"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    
    # Stats
    events_received = models.IntegerField(default=0)
    events_processed = models.IntegerField(default=0)
    events_failed = models.IntegerField(default=0)
    
    class Meta:
        unique_together = [('tenant', 'webhook_key')]
        ordering = ['-created_at']
        verbose_name = 'Webhook Subscription'
        verbose_name_plural = 'Webhook Subscriptions'
    
    def __str__(self):
        return f"{self.tenant.tenant_name} - {self.status}"
    
    def increment_received(self):
        """Increment events received counter."""
        self.events_received += 1
        self.last_event_at = timezone.now()
        self.save(update_fields=['events_received', 'last_event_at'])
    
    def increment_processed(self):
        """Increment events processed counter."""
        self.events_processed += 1
        self.save(update_fields=['events_processed'])
    
    def increment_failed(self):
        """Increment events failed counter."""
        self.events_failed += 1
        self.save(update_fields=['events_failed'])


class WebhookEvent(models.Model):
    """
    Logs incoming webhook events for processing and auditing.
    """
    
    STATUS_CHOICES = [
        ('received', 'Received'),
        ('processing', 'Processing'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]
    
    # Xero event categories
    EVENT_CATEGORIES = [
        ('INVOICE', 'Invoice'),
        ('CONTACT', 'Contact'),
        ('ACCOUNT', 'Account'),
        ('PAYMENT', 'Payment'),
        ('BANKTRANSACTION', 'Bank Transaction'),
        ('CREDITNOTE', 'Credit Note'),
        ('PREPAYMENT', 'Prepayment'),
        ('OVERPAYMENT', 'Overpayment'),
        ('MANUALJOURNAL', 'Manual Journal'),
        ('OTHER', 'Other'),
    ]
    
    subscription = models.ForeignKey(
        WebhookSubscription, 
        on_delete=models.CASCADE, 
        related_name='events'
    )
    
    # Event identification
    event_id = models.CharField(
        max_length=255,
        help_text="Unique event identifier from Xero"
    )
    resource_id = models.CharField(
        max_length=255,
        help_text="ID of the affected resource (e.g., InvoiceID)"
    )
    event_category = models.CharField(
        max_length=50,
        choices=EVENT_CATEGORIES,
        default='OTHER'
    )
    event_type = models.CharField(
        max_length=50,
        help_text="Type of event (e.g., CREATE, UPDATE, DELETE)"
    )
    
    # Event data
    payload = models.JSONField(
        blank=True,
        null=True,
        help_text="Full webhook payload"
    )
    
    # Processing status
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='received'
    )
    error_message = models.TextField(blank=True)
    retry_count = models.IntegerField(default=0)
    
    # Timestamps
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = [('subscription', 'event_id')]
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['subscription', 'status'], name='wh_evt_sub_status_idx'),
            models.Index(fields=['event_category', 'status'], name='wh_evt_cat_status_idx'),
            models.Index(fields=['resource_id'], name='wh_evt_resource_idx'),
        ]
        verbose_name = 'Webhook Event'
        verbose_name_plural = 'Webhook Events'
    
    def __str__(self):
        return f"{self.event_category} {self.event_type}: {self.resource_id} ({self.status})"
    
    def mark_processing(self):
        """Mark event as being processed."""
        self.status = 'processing'
        self.save(update_fields=['status'])
    
    def mark_processed(self):
        """Mark event as successfully processed."""
        self.status = 'processed'
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'processed_at'])
        self.subscription.increment_processed()
    
    def mark_failed(self, error_message):
        """Mark event as failed with error message."""
        self.status = 'failed'
        self.error_message = error_message
        self.retry_count += 1
        self.save(update_fields=['status', 'error_message', 'retry_count'])
        self.subscription.increment_failed()
    
    def mark_skipped(self, reason=''):
        """Mark event as skipped."""
        self.status = 'skipped'
        self.error_message = reason
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'error_message', 'processed_at'])
