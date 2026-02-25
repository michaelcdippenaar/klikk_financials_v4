"""
Service layer for xero_data-related business logic.
Handles updating transaction data from Xero API.
Note: All API calls are sequential to respect Xero's 5 concurrent call limit.

Trail balance is built from:
- Invoices (sales + bills), bank transactions, payments, credit notes,
  prepayments, overpayments, and manual journals.
- Data is per contact (from each transaction's Contact) and per tracking
  category (from line-level Tracking on invoices, bank txns, credit notes).

Usage:
    update_financial_data(tenant_id)
    update_xero_transactions(tenant_id)  # Fetch only, no journal processing
"""
import time
import logging

from django.conf import settings

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi
from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken

logger = logging.getLogger(__name__)


def _get_credentials_for_tenant(tenant_id, user=None):
    """
    Find credentials that have a token for the given tenant.
    
    Args:
        tenant_id: Xero tenant ID
        user: Optional user to prefer credentials for
    
    Returns:
        XeroClientCredentials instance
    
    Raises:
        ValueError: If no credentials found for tenant
    """
    credentials = None
    
    if user:
        # Try to find credentials for the provided user that have a token for this tenant
        user_credentials = XeroClientCredentials.objects.filter(user=user, active=True)
        for cred in user_credentials:
            if cred.get_tenant_token_data(tenant_id):
                credentials = cred
                break
    
    # If not found, try to find any active credentials that have a token for this tenant
    if not credentials:
        all_credentials = XeroClientCredentials.objects.filter(active=True)
        for cred in all_credentials:
            if cred.get_tenant_token_data(tenant_id):
                credentials = cred
                break
    
    # If still not found, check XeroTenantToken model for backward compatibility
    if not credentials:
        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
            tenant_token = XeroTenantToken.objects.filter(tenant=tenant, credentials__active=True).first()
            if tenant_token:
                credentials = tenant_token.credentials
        except XeroTenant.DoesNotExist:
            pass
    
    if not credentials:
        raise ValueError(f"No active credentials found with token for tenant {tenant_id}. Please re-authenticate this tenant.")
    
    return credentials


def update_xero_transactions(tenant_id, user=None, load_all=False):
    """
    Fetch all transaction data from Xero API (transaction-based pipeline, no Journals API).
    
    This fetches:
    - Invoices (sales and purchase)
    - Bank Transactions (spend and receive)
    - Payments
    - Credit Notes
    - Prepayments
    - Overpayments
    - Manual Journals (Manual Journals API only)
    
    Args:
        tenant_id: Xero tenant ID
        user: User object (optional)
        load_all: If True, manual journals are loaded in full (ignore modified_since). Default False.
    
    Returns:
        dict: Result with status, message, errors, and stats
    """
    start_time = time.time()
    
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    credentials = _get_credentials_for_tenant(tenant_id, user)
    user = credentials.user
    
    stats = {
        'invoices_updated': 0,
        'bank_transactions_updated': 0,
        'payments_updated': 0,
        'credit_notes_updated': 0,
        'prepayments_updated': 0,
        'overpayments_updated': 0,
        'manual_journals_updated': 0,
        'api_calls': 0,
    }
    
    errors = []
    
    try:
        api_client = XeroApiClient(user, tenant_id=tenant_id)
        touched_transaction_ids = set()
        xero_api = XeroAccountingApi(api_client, tenant_id, touched_transaction_ids=touched_transaction_ids)
        stats['api_calls'] += 1
        
        # Transaction calls (sequential). Manual Journals only; deprecated Journals API not used.
        transaction_calls = [
            ('invoices', lambda: xero_api.invoices().get()),
            ('bank_transactions', lambda: xero_api.bank_transactions().get()),
            ('payments', lambda: xero_api.payments().get()),
            ('credit_notes', lambda: xero_api.credit_notes().get()),
            ('prepayments', lambda: xero_api.prepayments().get()),
            ('overpayments', lambda: xero_api.overpayments().get()),
            ('manual_journals', lambda: xero_api.manual_journals(load_all=load_all).get()),
        ]
        
        for name, call in transaction_calls:
            try:
                call()
                stats[f'{name}_updated'] = 1
                stats['api_calls'] += 1
            except Exception as e:
                error_msg = f"Failed to update {name}: {str(e)}"
                logger.error("Failed to update %s: %s", name, str(e))
                errors.append(error_msg)
        
        duration = time.time() - start_time
        stats['duration_seconds'] = duration
        stats['total_errors'] = len(errors)

        if settings.DEBUG:
            ok = [n for n, _ in transaction_calls if stats.get(f'{n}_updated')]
            fail = [n for n, _ in transaction_calls if not stats.get(f'{n}_updated')]
            touched = len(touched_transaction_ids) if touched_transaction_ids else 0
            print(
                "[Sync] Fetched: %s (%.2fs) | touched_transaction_ids=%d | errors=%d"
                % (', '.join(ok), duration, touched, len(errors))
            )
            if fail:
                print("[Sync] Failed: %s" % ', '.join(fail))
        
        result = {
            'success': len(errors) == 0,
            'message': f"Transaction data updated for tenant {tenant_id}",
            'errors': errors,
            'stats': stats
        }
        result['touched_transaction_ids'] = touched_transaction_ids
        return result
        
    except ValueError as e:
        duration = time.time() - start_time
        error_msg = f"Authentication error for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise ValueError(error_msg) from e
    except Exception as e:
        duration = time.time() - start_time
        error_msg = f"Failed to update transactions for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise Exception(error_msg) from e


def update_financial_data(tenant_id, user=None, load_all=False):
    """
    Fetch transaction data from Xero API. Journal processing is handled
    separately by process_xero_data() to avoid duplicate work.
    
    Args:
        tenant_id: Xero tenant ID
        user: User object (optional)
        load_all: If True, manual journals are loaded in full (ignore modified_since). Default False.
    
    Returns:
        dict: Result with status, message, errors, stats, and touched_transaction_ids
    """
    return update_xero_transactions(tenant_id, user, load_all=load_all)


def update_and_consolidate(tenant_id, user=None):
    """
    Update financial data and consolidate to trail balance.
    
    Workflow:
    1. Fetch transactions and Manual Journals from Xero
    2. Process to journal entries
    3. Consolidate to XeroTrailBalance
    
    Args:
        tenant_id: Xero tenant ID
        user: User object (optional)
    
    Returns:
        dict: Result with full pipeline stats
    """
    from apps.xero.xero_data.models import XeroJournals
    from apps.xero.xero_cube.models import XeroTrailBalance
    
    start_time = time.time()
    
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return {'success': False, 'error': f'Tenant {tenant_id} not found'}
    
    result = {
        'success': True,
        'tenant_id': tenant_id,
        'tenant_name': tenant.tenant_name,
        'errors': [],
        'stats': {}
    }
    
    # Step 1: Update financial data
    try:
        update_result = update_financial_data(tenant_id, user)
        result['stats']['fetch'] = update_result.get('stats', {})
        if update_result.get('errors'):
            result['errors'].extend(update_result['errors'])
    except Exception as e:
        error_msg = f"Failed to fetch financial data: {str(e)}"
        logger.error(error_msg, exc_info=True)
        result['errors'].append(error_msg)
        result['success'] = False
        return result
    
    # Step 2: Consolidate to trail balance (full rebuild via SQL)
    try:
        tb_result = XeroTrailBalance.objects.consolidate_journals(tenant)

        result['stats']['trail_balance_records'] = tb_result.count()
        if settings.DEBUG:
            print("[Sync] Consolidate: %d trail balance records" % tb_result.count())
    except Exception as e:
        error_msg = f"Failed to consolidate trail balance: {str(e)}"
        logger.error(error_msg, exc_info=True)
        result['errors'].append(error_msg)
        result['success'] = False
    
    duration = time.time() - start_time
    result['stats']['total_duration_seconds'] = duration
    result['success'] = len(result['errors']) == 0
    
    if settings.DEBUG:
        print("[Sync] Consolidate complete in %.2fs. Success: %s" % (duration, result['success']))
    
    return result
