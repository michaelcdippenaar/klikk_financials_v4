"""
Xero-specific Process Tree Builder.

Provides helper functions to build Xero sync process trees using ProcessTreeBuilder.
"""
from apps.xero.xero_sync.process_manager.tree_builder import ProcessTreeBuilder
from apps.xero.xero_metadata.services import update_metadata
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_cube.services import process_xero_data, process_profit_loss
from apps.xero.xero_core.models import XeroTenant
import logging

logger = logging.getLogger(__name__)


def build_xero_sync_tree(tenant_id: str, user=None) -> ProcessTreeBuilder:
    """
    Build a Xero sync process tree using ProcessTreeBuilder.
    
    Args:
        tenant_id: Xero tenant ID
        user: Optional user object for API authentication
    
    Returns:
        ProcessTreeBuilder instance (call .save() to save to database)
    """
    # Get user from credentials if not provided
    if not user:
        credentials = XeroClientCredentials.objects.filter(active=True).first()
        if not credentials:
            raise ValueError("No active credentials found")
        user = credentials.user
    
    # Get tenant
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    # Define process functions
    def fetch_metadata(**context):
        """Fetch metadata (accounts, contacts, tracking categories)."""
        logger.info(f"Fetching metadata for tenant {tenant_id}")
        result = update_metadata(tenant_id, user=user)
        if not result.get('success'):
            raise Exception(f"Metadata update failed: {result.get('message', 'Unknown error')}")
        return result
    
    def fetch_journals(**context):
        """Fetch manual journals (Journals API removed)."""
        logger.info(f"Fetching manual journals for tenant {tenant_id}")
        api_client = XeroApiClient(user, tenant_id=tenant_id)
        xero_api = XeroAccountingApi(api_client, tenant_id)
        xero_api.manual_journals(load_all=False).get()
        return {'status': 'success', 'endpoint': 'manual_journals'}
    
    def fetch_manual_journals(**context):
        """Fetch manual journals."""
        logger.info(f"Fetching manual journals for tenant {tenant_id}")
        api_client = XeroApiClient(user, tenant_id=tenant_id)
        xero_api = XeroAccountingApi(api_client, tenant_id)
        xero_api.manual_journals(load_all=False).get()
        return {'status': 'success', 'endpoint': 'manual_journals'}
    
    def process_data(**context):
        """Process data (journals -> trail balance -> P&L balance_to_date)."""
        logger.info(f"Processing data for tenant {tenant_id}")
        result = process_xero_data(tenant_id)
        if not result.get('success'):
            raise Exception(f"Data processing failed: {result.get('message', 'Unknown error')}")
        return result
    
    def process_pnl(**context):
        """Process Profit & Loss (import and validate)."""
        logger.info(f"Processing P&L for tenant {tenant_id}")
        result = process_profit_loss(tenant_id, user=user)
        if not result.get('success'):
            raise Exception(f"P&L processing failed: {result.get('message', 'Unknown error')}")
        return result
    
    # Validation functions
    def validate_metadata_result(result):
        """Validate metadata update result."""
        if not isinstance(result, dict):
            return False, "Result must be a dictionary"
        if not result.get('success'):
            return False, result.get('message', 'Metadata update failed')
        stats = result.get('stats', {})
        if stats.get('total_errors', 0) > 0:
            return False, f"Metadata update had {stats.get('total_errors')} errors"
        return True, None
    
    def validate_data_result(result):
        """Validate data processing result."""
        if not isinstance(result, dict):
            return False, "Result must be a dictionary"
        if not result.get('success'):
            return False, result.get('message', 'Data processing failed')
        stats = result.get('stats', {})
        if not stats.get('trail_balance_created', False):
            return False, "Trail balance was not created"
        return True, None
    
    def validate_pnl_result(result):
        """Validate P&L processing result."""
        if not isinstance(result, dict):
            return False, "Result must be a dictionary"
        if not result.get('success'):
            return False, result.get('message', 'P&L processing failed')
        stats = result.get('stats', {})
        if not stats.get('pnl_imported', False):
            return False, "P&L was not imported"
        if not stats.get('in_sync', True):
            return False, "P&L validation failed - out of sync"
        return True, None
    
    # Build process tree
    builder = ProcessTreeBuilder(
        name=f'xero_sync_{tenant_id}',
        description=f'Xero sync workflow for tenant {tenant_id}',
        cache_enabled=True
    )
    
    # Add processes
    builder.add(
        'fetch_metadata',
        func=fetch_metadata,
        dependencies=[],
        cache_key=f'xero_metadata_{tenant_id}',
        cache_ttl=3600,
        validation=validate_metadata_result,
        required=True,
        metadata={'type': 'metadata', 'endpoints': ['accounts', 'contacts', 'tracking_categories']},
        response_vars={
            'success': {'type': bool, 'default': False, 'key': 'success'},
            'accounts_updated': {'type': int, 'default': 0, 'extract_func': lambda r: r.get('stats', {}).get('accounts_updated', 0) if isinstance(r, dict) else 0},
            'contacts_updated': {'type': int, 'default': 0, 'extract_func': lambda r: r.get('stats', {}).get('contacts_updated', 0) if isinstance(r, dict) else 0},
            'duration_seconds': {'type': float, 'default': 0.0, 'extract_func': lambda r: r.get('stats', {}).get('duration_seconds', 0.0) if isinstance(r, dict) else 0.0},
        }
    ).add(
        'fetch_journals',
        func=fetch_journals,
        dependencies=['fetch_metadata'],
        cache_key=f'xero_journals_{tenant_id}',
        cache_ttl=1800,
        validation=lambda r: (isinstance(r, dict) and r.get('status') == 'success', None),
        required=True,
        metadata={'type': 'data_source', 'endpoint': 'journals'},
        response_vars={
            'status': {'type': str, 'default': 'pending', 'key': 'status'},
        }
    ).add(
        'fetch_manual_journals',
        func=fetch_manual_journals,
        dependencies=['fetch_metadata'],
        cache_key=f'xero_manual_journals_{tenant_id}',
        cache_ttl=1800,
        validation=lambda r: (isinstance(r, dict) and r.get('status') == 'success', None),
        required=True,
        metadata={'type': 'data_source', 'endpoint': 'manual_journals'},
        response_vars={
            'status': {'type': str, 'default': 'pending', 'key': 'status'},
        }
    ).add(
        'process_data',
        func=process_data,
        dependencies=['fetch_journals', 'fetch_manual_journals'],
        cache_key=f'xero_process_data_{tenant_id}',
        cache_ttl=900,
        validation=validate_data_result,
        required=True,
        metadata={'type': 'processing', 'steps': ['process_journals', 'create_trail_balance']},
        response_vars={
            'success': {'type': bool, 'default': False, 'key': 'success'},
            'trail_balance_created': {'type': bool, 'default': False, 'extract_func': lambda r: r.get('stats', {}).get('trail_balance_created', False) if isinstance(r, dict) else False},
        }
    ).add(
        'process_pnl',
        func=process_pnl,
        dependencies=['process_data'],
        cache_key=f'xero_pnl_{tenant_id}',
        cache_ttl=600,
        validation=validate_pnl_result,
        required=True,
        metadata={'type': 'validation', 'steps': ['import_pnl', 'validate_pnl']},
        response_vars={
            'success': {'type': bool, 'default': False, 'key': 'success'},
            'in_sync': {'type': bool, 'default': True, 'extract_func': lambda r: r.get('stats', {}).get('in_sync', True) if isinstance(r, dict) else True},
        }
    )
    
    return builder


