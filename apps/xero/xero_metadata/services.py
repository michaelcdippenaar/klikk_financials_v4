"""
Xero metadata services - handles updating accounts, contacts, and tracking categories.
"""
import time
import logging
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi
from apps.xero.xero_auth.models import XeroClientCredentials

logger = logging.getLogger(__name__)


def update_metadata(tenant_id, user=None):
    """
    Update metadata (accounts, contacts, tracking categories) from Xero API.
    
    Args:
        tenant_id: Xero tenant ID
        user: User object (optional, will use first active credentials if not provided)
    
    Returns:
        dict: Result with status, message, errors, and stats
    """
    print("[METADATA] Starting metadata update process")
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
        'organisation_updated': 0,
        'accounts_updated': 0,
        'tracking_categories_updated': 0,
        'contacts_updated': 0,
        'api_calls': 0,
    }
    
    errors = []
    
    try:
        api_client = XeroApiClient(user, tenant_id=tenant_id)
        xero_api = XeroAccountingApi(api_client, tenant_id)
        stats['api_calls'] += 1  # Initial API client creation

        # Define metadata API calls (executed sequentially to respect Xero's 5 concurrent call limit)
        # Organisation first - fetches fiscal year settings from Xero
        metadata_calls = [
            ('organisation', lambda: xero_api.organisation().get()),
            ('accounts', lambda: xero_api.accounts().get()),
            ('tracking_categories', lambda: xero_api.tracking_categories().get()),
            ('contacts', lambda: xero_api.contacts().get()),
        ]
        
        # Execute metadata calls sequentially
        print(f"[METADATA] Starting metadata updates: accounts, tracking_categories, contacts")
        stats['api_calls'] += len(metadata_calls)  # Count API calls
        
        from apps.xero.xero_sync.models import XeroLastUpdate
        
        for name, call in metadata_calls:
            try:
                # Execute the update
                call()
                stats[f'{name}_updated'] = 1  # Track that it completed
                
                # Update timestamp only on successful completion
                XeroLastUpdate.objects.update_or_create_timestamp(name, tenant)
                print(f"[METADATA] ✓ {name} finished")
                logger.info(f"Successfully updated {name} for tenant {tenant_id}")
            except Exception as e:
                error_msg = f"Failed to update {name}: {str(e)}"
                print(f"[METADATA] ✗ {name} failed: {str(e)}")
                logger.error(error_msg, exc_info=True)
                errors.append(error_msg)
                # Don't update timestamp on error - preserve last successful date
        print(f"[METADATA] Metadata updates completed")
        
        duration = time.time() - start_time
        
        return {
            'success': len(errors) == 0,
            'message': f"Metadata updated for tenant {tenant_id}" if len(errors) == 0 else f"Metadata update completed with {len(errors)} errors",
            'errors': errors,
            'stats': {
                **stats,
                'duration_seconds': duration,
                'total_errors': len(errors)
            }
        }
        
    except Exception as e:
        duration = time.time() - start_time
        error_msg = f"Failed to update metadata: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            'success': False,
            'message': error_msg,
            'errors': [error_msg],
            'stats': {
                **stats,
                'duration_seconds': duration,
                'total_errors': 1
            }
        }

