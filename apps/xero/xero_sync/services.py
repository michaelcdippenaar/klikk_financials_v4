"""
Xero sync services - data synchronization from Xero API.
Note: All API calls are sequential to respect Xero's 5 concurrent call limit.
"""
import time
import logging
from django.db.models import Q

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi
from apps.xero.xero_auth.models import XeroClientCredentials

logger = logging.getLogger(__name__)


def update_xero_models(tenant_id, user=None):
    """
    Service function to update Xero models from API.
    Extracted from XeroUpdateModelsView for use in scheduled tasks.
    
    Args:
        tenant_id: Xero tenant ID
        user: User object (optional, will use first active credentials if not provided)
    
    Returns:
        dict: Result with status, message, errors, and stats
    """
    start_time = time.time()
    
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    # Get user from tenant's credentials if not provided
    if not user:
        credentials = XeroClientCredentials.objects.filter(active=True).first()
        if not credentials:
            raise ValueError("No active credentials found")
        user = credentials.user
    
    stats = {
        'accounts_updated': 0,
        'tracking_categories_updated': 0,
        'contacts_updated': 0,
        'bank_transactions_updated': 0,
        'invoices_updated': 0,
        'payments_updated': 0,
        'journals_updated': 0,
        'api_calls': 0,
    }
    
    errors = []
    
    # Group 1: Metadata calls - delegate to xero_metadata app
    print(f"[UPDATE] Starting Group 1: accounts, tracking_categories, contacts")
    try:
        from apps.xero.xero_metadata.services import update_metadata
        metadata_result = update_metadata(tenant_id, user=user)
        
        # Update stats from metadata result
        metadata_stats = metadata_result.get('stats', {})
        stats['accounts_updated'] = metadata_stats.get('accounts_updated', 0)
        stats['tracking_categories_updated'] = metadata_stats.get('tracking_categories_updated', 0)
        stats['contacts_updated'] = metadata_stats.get('contacts_updated', 0)
        stats['api_calls'] += metadata_stats.get('api_calls', 0)
        
        # Collect any errors from metadata update
        if metadata_result.get('errors'):
            errors.extend(metadata_result['errors'])
        
        if metadata_result['success']:
            print(f"[UPDATE] ✓ Metadata updates completed")
            logger.info(f"Successfully updated metadata for tenant {tenant_id}")
        else:
            print(f"[UPDATE] ✗ Metadata updates completed with errors")
            logger.warning(f"Metadata update completed with errors for tenant {tenant_id}")
    except Exception as e:
        error_msg = f"Failed to update metadata: {str(e)}"
        print(f"[UPDATE] ✗ Metadata update failed: {str(e)}")
        logger.error(error_msg, exc_info=True)
        errors.append(error_msg)
    print(f"[UPDATE] Group 1 completed")
    
    # Group 2: Transaction-related calls
    # Create API client for transaction calls
    try:
        api_client = XeroApiClient(user, tenant_id=tenant_id)
        xero_api = XeroAccountingApi(api_client, tenant_id)
        stats['api_calls'] += 1  # Initial API client creation
        
        transaction_calls = [
            ('bank_transactions', lambda: xero_api.bank_transactions().get()),
            ('invoices', lambda: xero_api.invoices().get()),
            ('payments', lambda: xero_api.payments().get()),
        ]
        
        # Execute Group 2 sequentially (after Group 1 completes)
        print(f"[UPDATE] Starting Group 2: bank_transactions, invoices, payments")
        stats['api_calls'] += len(transaction_calls)  # Count API calls
        for name, call in transaction_calls:
            try:
                call()
                stats[f'{name}_updated'] = 1
                print(f"[UPDATE] ✓ {name} finished")
                logger.info(f"Successfully updated {name} for tenant {tenant_id}")
            except Exception as e:
                error_msg = f"Failed to update {name}: {str(e)}"
                print(f"[UPDATE] ✗ {name} failed: {str(e)}")
                logger.error(error_msg)
                errors.append(error_msg)
        print(f"[UPDATE] Group 2 completed")
        
        # Manual journals only (deprecated Journals API not used)
        print(f"[UPDATE] Starting manual journals update")
        stats['api_calls'] += 1
        try:
            xero_api.manual_journals().get()
            stats['journals_updated'] = 1
            print(f"[UPDATE] ✓ manual journals finished")
            logger.info(f"Successfully updated manual journals for tenant {tenant_id}")
        except Exception as e:
            error_msg = f"Failed to update manual journals: {str(e)}"
            print(f"[UPDATE] ✗ manual journals failed: {str(e)}")
            logger.error(error_msg)
            errors.append(error_msg)
        
        duration = time.time() - start_time
        stats['duration_seconds'] = duration
        stats['total_errors'] = len(errors)
        
        print(f"[UPDATE] All updates completed in {duration:.2f} seconds. Errors: {len(errors)}")
        
        messages = [f"Data updated for tenant {tenant_id}"]
        
        return {
            'success': len(errors) == 0,
            'message': '. '.join(messages),
            'errors': errors,
            'stats': stats
        }
        
    except ValueError as e:
        # Handle authentication/token errors specifically
        duration = time.time() - start_time
        error_msg = f"Authentication error for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        # Re-raise as ValueError to distinguish from other exceptions
        raise ValueError(error_msg) from e
    except Exception as e:
        duration = time.time() - start_time
        error_msg = f"Failed to update data for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise Exception(error_msg) from e

