"""
Xero-specific Process Dependency Manager integration.

This module provides a ready-to-use ProcessManagerInstance configured
for Xero data processing workflows.
"""
from .wrapper import ProcessManagerInstance
from apps.xero.xero_metadata.services import update_metadata
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_cube.services import process_xero_data, process_profit_loss
from apps.xero.xero_core.models import XeroTenant
import logging

logger = logging.getLogger(__name__)


def create_xero_sync_instance(tenant_id: str, user=None):
    """
    Create a ProcessManagerInstance configured for Xero sync workflow.
    
    This provides a class-based interface with automatic method generation.
    
    Args:
        tenant_id: Xero tenant ID
        user: Optional user object for API authentication
    
    Returns:
        ProcessManagerInstance configured for Xero sync
    
    Example:
        instance = create_xero_sync_instance(tenant_id='123')
        results = instance.execute_tree('xero_sync')
        print(instance.fetch_metadata_success)
        print(instance.process_data_trail_balance_created)
    """
    from .wrapper import ProcessManagerInstance
    
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
    
    # Define process functions with tenant context
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
    
    # Define process tree
    process_trees = {
        'xero_sync': {
            # Step 1: Metadata (can run in parallel)
            'fetch_metadata': {
                'func': fetch_metadata,
                'dependencies': [],
                'cache_key': f'xero_metadata_{tenant_id}',
                'cache_ttl': 3600,  # Cache for 1 hour
                'validation': validate_metadata_result,
                'required': True,
                'metadata': {
                    'type': 'metadata',
                    'endpoints': ['accounts', 'contacts', 'tracking_categories']
                }
            },
            
            # Step 2: Data Source (depends on metadata, can run in parallel)
            'fetch_journals': {
                'func': fetch_journals,
                'dependencies': ['fetch_metadata'],
                'cache_key': f'xero_journals_{tenant_id}',
                'cache_ttl': 1800,  # Cache for 30 minutes
                'validation': lambda r: (
                    isinstance(r, dict) and r.get('status') == 'success',
                    None if isinstance(r, dict) and r.get('status') == 'success' else "Journals fetch failed"
                ),
                'required': True,
                'metadata': {'type': 'data_source', 'endpoint': 'journals'}
            },
            'fetch_manual_journals': {
                'func': fetch_manual_journals,
                'dependencies': ['fetch_metadata'],
                'cache_key': f'xero_manual_journals_{tenant_id}',
                'cache_ttl': 1800,  # Cache for 30 minutes
                'validation': lambda r: (
                    isinstance(r, dict) and r.get('status') == 'success',
                    None if isinstance(r, dict) and r.get('status') == 'success' else "Manual journals fetch failed"
                ),
                'required': True,
                'metadata': {'type': 'data_source', 'endpoint': 'manual_journals'}
            },
            
            # Step 3: Process Data (depends on data source)
            'process_data': {
                'func': process_data,
                'dependencies': ['fetch_journals', 'fetch_manual_journals'],
                'cache_key': f'xero_process_data_{tenant_id}',
                'cache_ttl': 900,  # Cache for 15 minutes
                'validation': validate_data_result,
                'required': True,
                'metadata': {
                    'type': 'processing',
                    'steps': ['process_journals', 'create_trail_balance', 'calculate_pnl_balance_to_date']
                }
            },
            
            # Step 4: Profit & Loss (depends on process data)
            'process_pnl': {
                'func': process_pnl,
                'dependencies': ['process_data'],
                'cache_key': f'xero_pnl_{tenant_id}',
                'cache_ttl': 600,  # Cache for 10 minutes
                'validation': validate_pnl_result,
                'required': True,
                'metadata': {
                    'type': 'validation',
                    'steps': ['import_pnl', 'validate_pnl']
                }
            },
        }
    }
    
    # Define response variables (same as in create_xero_sync_workflow)
    response_variables = {
        'xero_sync': {
            'fetch_metadata': {
                'success': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether metadata update was successful',
                    'key': 'success'
                },
                'stats': {
                    'type': dict,
                    'default': {},
                    'description': 'Metadata update statistics',
                    'key': 'stats'
                },
                'accounts_updated': {
                    'type': int,
                    'default': 0,
                    'description': 'Number of accounts updated',
                    'extract_func': lambda r: r.get('stats', {}).get('accounts_updated', 0) if isinstance(r, dict) else 0
                },
                'contacts_updated': {
                    'type': int,
                    'default': 0,
                    'description': 'Number of contacts updated',
                    'extract_func': lambda r: r.get('stats', {}).get('contacts_updated', 0) if isinstance(r, dict) else 0
                },
                'tracking_categories_updated': {
                    'type': int,
                    'default': 0,
                    'description': 'Number of tracking categories updated',
                    'extract_func': lambda r: r.get('stats', {}).get('tracking_categories_updated', 0) if isinstance(r, dict) else 0
                },
                'duration_seconds': {
                    'type': float,
                    'default': 0.0,
                    'description': 'Metadata update duration in seconds',
                    'extract_func': lambda r: r.get('stats', {}).get('duration_seconds', 0.0) if isinstance(r, dict) else 0.0
                },
            },
            'fetch_journals': {
                'status': {
                    'type': str,
                    'default': 'pending',
                    'description': 'Journals fetch status',
                    'key': 'status'
                },
                'endpoint': {
                    'type': str,
                    'default': 'journals',
                    'description': 'Endpoint name',
                    'key': 'endpoint'
                },
            },
            'fetch_manual_journals': {
                'status': {
                    'type': str,
                    'default': 'pending',
                    'description': 'Manual journals fetch status',
                    'key': 'status'
                },
                'endpoint': {
                    'type': str,
                    'default': 'manual_journals',
                    'description': 'Endpoint name',
                    'key': 'endpoint'
                },
            },
            'process_data': {
                'success': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether data processing was successful',
                    'key': 'success'
                },
                'message': {
                    'type': str,
                    'default': '',
                    'description': 'Processing message',
                    'key': 'message'
                },
                'stats': {
                    'type': dict,
                    'default': {},
                    'description': 'Processing statistics',
                    'key': 'stats'
                },
                'journals_processed': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether journals were processed',
                    'extract_func': lambda r: r.get('stats', {}).get('journals_processed', False) if isinstance(r, dict) else False
                },
                'trail_balance_created': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether trail balance was created',
                    'extract_func': lambda r: r.get('stats', {}).get('trail_balance_created', False) if isinstance(r, dict) else False
                },
                'pnl_balance_to_date_calculated': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether P&L balance_to_date was calculated',
                    'extract_func': lambda r: r.get('stats', {}).get('pnl_balance_to_date_calculated', False) if isinstance(r, dict) else False
                },
                'duration_seconds': {
                    'type': float,
                    'default': 0.0,
                    'description': 'Processing duration in seconds',
                    'extract_func': lambda r: r.get('stats', {}).get('duration_seconds', 0.0) if isinstance(r, dict) else 0.0
                },
            },
            'process_pnl': {
                'success': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether P&L processing was successful',
                    'key': 'success'
                },
                'message': {
                    'type': str,
                    'default': '',
                    'description': 'P&L processing message',
                    'key': 'message'
                },
                'stats': {
                    'type': dict,
                    'default': {},
                    'description': 'P&L processing statistics',
                    'key': 'stats'
                },
                'pnl_imported': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether P&L was imported',
                    'extract_func': lambda r: r.get('stats', {}).get('pnl_imported', False) if isinstance(r, dict) else False
                },
                'pnl_validated': {
                    'type': bool,
                    'default': False,
                    'description': 'Whether P&L was validated',
                    'extract_func': lambda r: r.get('stats', {}).get('pnl_validated', False) if isinstance(r, dict) else False
                },
                'in_sync': {
                    'type': bool,
                    'default': True,
                    'description': 'Whether P&L is in sync',
                    'extract_func': lambda r: r.get('stats', {}).get('in_sync', True) if isinstance(r, dict) else True
                },
                'validation_errors': {
                    'type': int,
                    'default': 0,
                    'description': 'Number of validation errors',
                    'extract_func': lambda r: r.get('stats', {}).get('validation_errors', 0) if isinstance(r, dict) else 0
                },
                'duration_seconds': {
                    'type': float,
                    'default': 0.0,
                    'description': 'P&L processing duration in seconds',
                    'extract_func': lambda r: r.get('stats', {}).get('duration_seconds', 0.0) if isinstance(r, dict) else 0.0
                },
            },
        }
    }
    
    # Create instance
    instance = ProcessManagerInstance(
        process_trees=process_trees,
        cache_enabled=True,
        response_variables=response_variables
    )
    
    return instance


def check_xero_sync_status(tenant_id: str, **context) -> dict:
    """
    Check sync status for all Xero endpoints.
    
    This function checks the XeroLastUpdate model to see which endpoints
    are marked as out_of_sync=True.
    
    Args:
        tenant_id: Xero tenant ID
        **context: Additional context (unused but kept for compatibility)
    
    Returns:
        Dict with sync check results:
        {
            'out_of_sync': [list of endpoint/process names],
            'details': {endpoint_name: {'out_of_sync': bool, 'error': str}}
        }
    """
    from apps.xero.xero_sync.models import XeroLastUpdate
    from apps.xero.xero_core.models import XeroTenant
    
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    # Map of endpoint names to process names
    endpoint_to_process = {
        'accounts': 'fetch_metadata',
        'contacts': 'fetch_metadata',
        'tracking_categories': 'fetch_metadata',
        'journals': 'fetch_journals',
        'manual_journals': 'fetch_manual_journals',
        'trail_balance': 'process_data',
        'profit_loss': 'process_pnl',
    }
    
    # Check all endpoints
    out_of_sync_endpoints = []
    details = {}
    
    # Check metadata endpoints
    metadata_endpoints = ['accounts', 'contacts', 'tracking_categories']
    metadata_out_of_sync = False
    metadata_errors = []
    
    for endpoint in metadata_endpoints:
        try:
            last_update = XeroLastUpdate.objects.get(
                end_point=endpoint,
                organisation=tenant
            )
            if not last_update.date:
                metadata_out_of_sync = True
                metadata_errors.append(f"{endpoint}: Never updated")
                details[endpoint] = {
                    'out_of_sync': True,
                    'error': 'Never updated'
                }
            else:
                details[endpoint] = {
                    'out_of_sync': False,
                    'error': None
                }
        except XeroLastUpdate.DoesNotExist:
            # If no update record exists, consider it out of sync
            metadata_out_of_sync = True
            metadata_errors.append(f"{endpoint}: No update record found")
            details[endpoint] = {
                'out_of_sync': True,
                'error': 'No update record found'
            }
    
    if metadata_out_of_sync:
        out_of_sync_endpoints.append('fetch_metadata')
        details['fetch_metadata'] = {
            'out_of_sync': True,
            'error': '; '.join(metadata_errors)
        }
    else:
        details['fetch_metadata'] = {
            'out_of_sync': False,
            'error': None
        }
    
    # Check data source endpoints
    for endpoint, process_name in [('journals', 'fetch_journals'), ('manual_journals', 'fetch_manual_journals')]:
        try:
            last_update = XeroLastUpdate.objects.get(
                end_point=endpoint,
                organisation=tenant
            )
            if not last_update.date:
                out_of_sync_endpoints.append(process_name)
                details[process_name] = {
                    'out_of_sync': True,
                    'error': 'Never updated'
                }
            else:
                details[process_name] = {
                    'out_of_sync': False,
                    'error': None
                }
        except XeroLastUpdate.DoesNotExist:
            out_of_sync_endpoints.append(process_name)
            details[process_name] = {
                'out_of_sync': True,
                'error': 'No update record found'
            }
    
    # Check trail balance
    # Since out_of_sync field was removed, consider out of sync if date is None
    try:
        last_update = XeroLastUpdate.objects.get(
            end_point='trail_balance',
            organisation=tenant
        )
        if not last_update.date:
            out_of_sync_endpoints.append('process_data')
            details['process_data'] = {
                'out_of_sync': True,
                'error': 'Never updated'
            }
        else:
            details['process_data'] = {
                'out_of_sync': False,
                'error': None
            }
    except XeroLastUpdate.DoesNotExist:
        out_of_sync_endpoints.append('process_data')
        details['process_data'] = {
            'out_of_sync': True,
            'error': 'No update record found'
        }
    
    # Check profit loss
    # Since out_of_sync field was removed, consider out of sync if date is None
    try:
        last_update = XeroLastUpdate.objects.get(
            end_point='profit_loss',
            organisation=tenant
        )
        if not last_update.date:
            out_of_sync_endpoints.append('process_pnl')
            details['process_pnl'] = {
                'out_of_sync': True,
                'error': 'Never updated'
            }
        else:
            details['process_pnl'] = {
                'out_of_sync': False,
                'error': None
            }
    except XeroLastUpdate.DoesNotExist:
        out_of_sync_endpoints.append('process_pnl')
        details['process_pnl'] = {
            'out_of_sync': True,
            'error': 'No update record found'
        }
    
    return {
        'out_of_sync': out_of_sync_endpoints,
        'details': details
    }



