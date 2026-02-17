"""
Xero Webhook Services

This module provides services for:
- Webhook signature validation
- Event processing and routing
- Incremental data updates based on webhook events

Xero webhook signature validation:
https://developer.xero.com/documentation/guides/webhooks/signature-validation
"""
import hashlib
import hmac
import base64
import logging
from django.utils import timezone
from django.db import transaction

logger = logging.getLogger(__name__)


def validate_webhook_signature(payload, signature, webhook_key):
    """
    Validate a Xero webhook signature.
    
    Xero uses HMAC-SHA256 with the webhook key to sign payloads.
    The signature is provided in the x-xero-signature header.
    
    Args:
        payload: Raw request body (bytes)
        signature: Signature from x-xero-signature header
        webhook_key: Secret webhook key for this subscription
    
    Returns:
        bool: True if signature is valid
    """
    if not payload or not signature or not webhook_key:
        return False
    
    try:
        # Compute expected signature
        expected = hmac.new(
            webhook_key.encode('utf-8'),
            payload,
            hashlib.sha256
        ).digest()
        
        # Base64 encode to match Xero's format
        expected_b64 = base64.b64encode(expected).decode('utf-8')
        
        # Compare signatures (use hmac.compare_digest for timing attack resistance)
        return hmac.compare_digest(expected_b64, signature)
    except Exception as e:
        logger.error(f"Error validating webhook signature: {e}")
        return False


def parse_webhook_events(payload):
    """
    Parse webhook payload into individual events.
    
    Xero webhook payload structure:
    {
        "events": [
            {
                "resourceUrl": "https://api.xero.com/api.xro/2.0/Invoices/...",
                "resourceId": "invoice-uuid",
                "tenantId": "tenant-uuid",
                "tenantType": "ORGANISATION",
                "eventDateUtc": "2024-01-15T10:00:00Z",
                "eventType": "UPDATE",
                "eventCategory": "INVOICE"
            },
            ...
        ],
        "lastEventSequence": 12345,
        "entropy": "random-string"
    }
    
    Args:
        payload: Parsed JSON payload (dict)
    
    Returns:
        list: List of event dicts
    """
    if not payload or not isinstance(payload, dict):
        return []
    
    events = payload.get('events', [])
    return events


def get_event_category_from_resource_url(resource_url):
    """
    Extract event category from Xero resource URL.
    
    Args:
        resource_url: Full resource URL from webhook
    
    Returns:
        str: Event category (e.g., 'INVOICE', 'CONTACT')
    """
    if not resource_url:
        return 'OTHER'
    
    # Extract the resource type from URL pattern
    # https://api.xero.com/api.xro/2.0/Invoices/...
    url_lower = resource_url.lower()
    
    category_map = {
        '/invoices/': 'INVOICE',
        '/contacts/': 'CONTACT',
        '/accounts/': 'ACCOUNT',
        '/payments/': 'PAYMENT',
        '/banktransactions/': 'BANKTRANSACTION',
        '/creditnotes/': 'CREDITNOTE',
        '/prepayments/': 'PREPAYMENT',
        '/overpayments/': 'OVERPAYMENT',
        '/manualjournals/': 'MANUALJOURNAL',
    }
    
    for pattern, category in category_map.items():
        if pattern in url_lower:
            return category
    
    return 'OTHER'


@transaction.atomic
def process_webhook_payload(subscription, payload, raw_payload):
    """
    Process a webhook payload and create event records.
    
    Args:
        subscription: WebhookSubscription instance
        payload: Parsed JSON payload (dict)
        raw_payload: Raw request body for logging
    
    Returns:
        dict: Processing results
    """
    from apps.xero.xero_webhooks.models import WebhookEvent
    
    events = parse_webhook_events(payload)
    
    results = {
        'events_received': len(events),
        'events_created': 0,
        'events_skipped': 0,
        'errors': []
    }
    
    for event_data in events:
        try:
            event_id = event_data.get('resourceId', '')
            event_type = event_data.get('eventType', 'UNKNOWN')
            event_category = event_data.get('eventCategory', '')
            
            # If no category in payload, try to extract from URL
            if not event_category:
                event_category = get_event_category_from_resource_url(
                    event_data.get('resourceUrl', '')
                )
            
            # Check if event already exists (deduplication)
            existing = WebhookEvent.objects.filter(
                subscription=subscription,
                event_id=event_id
            ).first()
            
            if existing:
                results['events_skipped'] += 1
                continue
            
            # Create event record
            WebhookEvent.objects.create(
                subscription=subscription,
                event_id=event_id,
                resource_id=event_data.get('resourceId', ''),
                event_category=event_category,
                event_type=event_type,
                payload=event_data,
                status='received'
            )
            
            results['events_created'] += 1
            
        except Exception as e:
            error_msg = f"Error processing event: {str(e)}"
            logger.error(error_msg, exc_info=True)
            results['errors'].append(error_msg)
    
    # Update subscription stats
    subscription.events_received += results['events_created']
    subscription.last_event_at = timezone.now()
    subscription.save(update_fields=['events_received', 'last_event_at'])
    
    return results


class WebhookEventProcessor:
    """
    Processes webhook events and triggers incremental updates.
    """
    
    def __init__(self, event):
        """
        Initialize processor for a webhook event.
        
        Args:
            event: WebhookEvent instance
        """
        self.event = event
        self.subscription = event.subscription
        self.tenant = self.subscription.tenant
    
    def process(self):
        """
        Process the webhook event.
        
        Routes to appropriate handler based on event category.
        
        Returns:
            bool: True if processing succeeded
        """
        self.event.mark_processing()
        
        try:
            # Route to category-specific handler
            handler_map = {
                'INVOICE': self._handle_invoice_event,
                'CONTACT': self._handle_contact_event,
                'ACCOUNT': self._handle_account_event,
                'PAYMENT': self._handle_payment_event,
                'BANKTRANSACTION': self._handle_bank_transaction_event,
                'CREDITNOTE': self._handle_credit_note_event,
                'PREPAYMENT': self._handle_prepayment_event,
                'OVERPAYMENT': self._handle_overpayment_event,
                'MANUALJOURNAL': self._handle_manual_journal_event,
            }
            
            handler = handler_map.get(self.event.event_category, self._handle_unknown_event)
            handler()
            
            self.event.mark_processed()
            logger.info(f"Successfully processed webhook event {self.event.event_id}")
            return True
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to process webhook event {self.event.event_id}: {error_msg}")
            self.event.mark_failed(error_msg)
            return False
    
    def _fetch_and_update_resource(self, api_method_name, resource_id):
        """
        Fetch a single resource from Xero and update local data.
        
        Args:
            api_method_name: Name of the XeroAccountingApi method to call
            resource_id: ID of the resource to fetch
        """
        from apps.xero.xero_data.services import _get_credentials_for_tenant
        from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi
        
        credentials = _get_credentials_for_tenant(self.tenant.tenant_id)
        api_client = XeroApiClient(credentials.user, tenant_id=self.tenant.tenant_id)
        xero_api = XeroAccountingApi(api_client, self.tenant.tenant_id)
        
        # Call the appropriate API method
        api_method = getattr(xero_api, api_method_name, None)
        if api_method:
            api_method().get()
    
    def _handle_invoice_event(self):
        """Handle invoice webhook event."""
        logger.info(f"Processing INVOICE event for {self.event.resource_id}")
        
        if self.event.event_type == 'DELETE':
            # Handle deletion - mark as voided in local data
            self._mark_resource_deleted('Invoice', self.event.resource_id)
        else:
            # Fetch updated invoice
            self._fetch_and_update_resource('invoices', self.event.resource_id)
    
    def _handle_contact_event(self):
        """Handle contact webhook event."""
        logger.info(f"Processing CONTACT event for {self.event.resource_id}")
        self._fetch_and_update_resource('contacts', self.event.resource_id)
    
    def _handle_account_event(self):
        """Handle account webhook event."""
        logger.info(f"Processing ACCOUNT event for {self.event.resource_id}")
        self._fetch_and_update_resource('accounts', self.event.resource_id)
    
    def _handle_payment_event(self):
        """Handle payment webhook event."""
        logger.info(f"Processing PAYMENT event for {self.event.resource_id}")
        self._fetch_and_update_resource('payments', self.event.resource_id)
    
    def _handle_bank_transaction_event(self):
        """Handle bank transaction webhook event."""
        logger.info(f"Processing BANKTRANSACTION event for {self.event.resource_id}")
        self._fetch_and_update_resource('bank_transactions', self.event.resource_id)
    
    def _handle_credit_note_event(self):
        """Handle credit note webhook event."""
        logger.info(f"Processing CREDITNOTE event for {self.event.resource_id}")
        self._fetch_and_update_resource('credit_notes', self.event.resource_id)
    
    def _handle_prepayment_event(self):
        """Handle prepayment webhook event."""
        logger.info(f"Processing PREPAYMENT event for {self.event.resource_id}")
        self._fetch_and_update_resource('prepayments', self.event.resource_id)
    
    def _handle_overpayment_event(self):
        """Handle overpayment webhook event."""
        logger.info(f"Processing OVERPAYMENT event for {self.event.resource_id}")
        self._fetch_and_update_resource('overpayments', self.event.resource_id)
    
    def _handle_manual_journal_event(self):
        """Handle manual journal webhook event."""
        logger.info(f"Processing MANUALJOURNAL event for {self.event.resource_id}")
        self._fetch_and_update_resource('manual_journals', self.event.resource_id)
    
    def _handle_unknown_event(self):
        """Handle unknown event type."""
        logger.warning(f"Unknown event category: {self.event.event_category}")
        self.event.mark_skipped(f"Unknown category: {self.event.event_category}")
    
    def _mark_resource_deleted(self, resource_type, resource_id):
        """Mark a resource as deleted in local data."""
        from apps.xero.xero_data.models import XeroTransactionSource
        
        # Update transaction source status
        XeroTransactionSource.objects.filter(
            organisation=self.tenant,
            transactions_id=resource_id
        ).update(collection={'Status': 'DELETED', '_deleted': True})
        
        logger.info(f"Marked {resource_type} {resource_id} as deleted")


def process_pending_events(subscription=None, limit=100):
    """
    Process pending webhook events.
    
    Args:
        subscription: Optional - only process events for this subscription
        limit: Maximum number of events to process
    
    Returns:
        dict: Processing statistics
    """
    from apps.xero.xero_webhooks.models import WebhookEvent
    
    query = WebhookEvent.objects.filter(status='received')
    
    if subscription:
        query = query.filter(subscription=subscription)
    
    events = query[:limit]
    
    stats = {
        'total': len(events),
        'processed': 0,
        'failed': 0
    }
    
    for event in events:
        processor = WebhookEventProcessor(event)
        if processor.process():
            stats['processed'] += 1
        else:
            stats['failed'] += 1
    
    return stats
