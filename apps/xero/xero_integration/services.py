"""
Xero integration services - external system integrations and data distribution.
"""
import asyncio
import logging
import os
import pandas_gbq
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from google.oauth2 import service_account
from django.conf import settings

logger = logging.getLogger(__name__)

# Thread pool executor for I/O operations
_io_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='bigquery_io')


def get_google_credentials():
    """
    Get Google Cloud credentials from environment variable or settings.
    
    Returns:
        service_account.Credentials: Google Cloud service account credentials
    """
    # Try environment variable first (for staging/production)
    credentials_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    
    # Fallback to settings if available
    if not credentials_path and hasattr(settings, 'GOOGLE_APPLICATION_CREDENTIALS'):
        credentials_path = settings.GOOGLE_APPLICATION_CREDENTIALS
    
    # Fallback to default path for development (only if file exists)
    if not credentials_path:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        default_path = os.path.join(project_root, 'credentials', 'klick-financials01-81b1aeed281d.json')
        if os.path.exists(default_path):
            credentials_path = default_path
    # Fallback to v3 sibling project credentials (when running v4 and v3 is alongside)
    if not credentials_path:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        parent_dir = os.path.dirname(project_root)
        v3_creds = os.path.join(parent_dir, 'klikk_financials_v3', 'credentials', 'klick-financials01-81b1aeed281d.json')
        if os.path.exists(v3_creds):
            credentials_path = v3_creds
            logger.debug(f"Using Google credentials from v3 project: {credentials_path}")
    
    if not credentials_path:
        raise ValueError(
            "Google Cloud credentials not found. Set GOOGLE_APPLICATION_CREDENTIALS environment variable "
            "or place credentials file in project/credentials/ directory."
        )
    
    if not os.path.exists(credentials_path):
        raise FileNotFoundError(
            f"Google Cloud credentials file not found at: {credentials_path}. "
            "Please set GOOGLE_APPLICATION_CREDENTIALS environment variable to the correct path."
        )
    
    return service_account.Credentials.from_service_account_file(credentials_path)


def update_google_big_query(df, table_id):
    """Synchronous BigQuery export function."""
    project_id = 'klick-financials01'
    try:
        GS_CREDENTIALS = get_google_credentials()
        pandas_gbq.to_gbq(dataframe=df, destination_table=table_id, if_exists='replace', project_id=project_id,
                          credentials=GS_CREDENTIALS)
    except Exception as e:
        logger.error(f"Failed to export to BigQuery: {str(e)}")
        raise


async def update_google_big_query_async(df, table_id):
    """
    Asynchronous BigQuery export function.
    Runs the synchronous export in a thread pool to avoid blocking.
    
    Args:
        df: pandas DataFrame to export
        table_id: BigQuery table ID
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_io_executor, update_google_big_query, df, table_id)
    logger.info(f"Async export completed for table {table_id}")


async def update_google_big_query_batch_async(exports):
    """
    Export multiple DataFrames to BigQuery in parallel.
    
    Args:
        exports: List of tuples (df, table_id)
    
    Example:
        exports = [
            (df_accounts, 'Xero.Accounts_123'),
            (df_rollup, 'Xero.AccountRollups_123'),
            (df_combined, 'Xero.AccountsWithRollups_123'),
        ]
        await update_google_big_query_batch_async(exports)
    """
    tasks = [
        update_google_big_query_async(df, table_id)
        for df, table_id in exports
    ]
    await asyncio.gather(*tasks)
    logger.info(f"Completed batch export of {len(exports)} tables")


def run_async_export(coro):
    """
    Helper function to run async exports from sync context.
    Handles both cases: new event loop or existing event loop.
    """
    try:
        # Try to get existing event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If loop is running, we're in an async context
            # Create a task and run in executor as fallback
            raise RuntimeError("Event loop is already running")
        else:
            # Loop exists but not running - use it
            return loop.run_until_complete(coro)
    except RuntimeError:
        # No event loop exists or loop is running - create new one
        try:
            return asyncio.run(coro)
        except RuntimeError:
            # If asyncio.run fails (loop already running), fallback to sync
            raise


def export_accounts(tenant_id):
    """
    Export XeroAccount data and rollup summary to Google BigQuery, including a combined table.

    Args:
        tenant_id (str): The ID of the tenant to export accounts for.
    """
    from apps.xero.xero_core.models import XeroTenant
    from apps.xero.xero_metadata.models import XeroAccount

    print('start export_accounts')

    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    accounts = XeroAccount.objects.filter(organisation=organisation).select_related(
        'business_unit', 'organisation'
    )
    if not accounts.exists():
        logger.warning(f"No accounts found for tenant {tenant_id}")
        return
    print('start export_accounts 2')
    print(accounts)
    # Export raw accounts
    df_accounts = accounts.to_dataframe([
        'organisation__tenant_id',
        'organisation__tenant_name',
        'account_id',
        'business_unit__division_code',
        'business_unit__division_description',
        'business_unit__business_unit_code',
        'business_unit__business_unit_description',
        'reporting_code',
        'reporting_code_name',
        'bank_account_number',
        'grouping',
        'code',
        'name',
        'type',
        'attr_entry_type',
        'attr_occurrence'
    ])

    print('start export_accounts 3')
    table_id_accounts = f'Xero.Accounts_{tenant_id.replace("-", "_")}'

    print('start export_accounts 4')

    # Process account rollups
    try:
        # Note: rollups functionality would need to be migrated separately if it exists
        # For now, create a simple rollup
        df_rollup = df_accounts.groupby([
            'organisation__tenant_id',
            'organisation__tenant_name',
            'type',
            'grouping'
        ]).agg({
            'account_id': 'count',
            'name': lambda x: list(x)[:5]  # Sample of account names
        }).reset_index()
        df_rollup.rename(columns={'account_id': 'account_count'}, inplace=True)

        table_id_rollup = f'Xero.AccountRollups_{tenant_id.replace("-", "_")}'

        # Create combined table
        df_accounts['record_type'] = 'account'
        df_rollup['record_type'] = 'rollup'
        # Ensure common columns for union
        common_columns = [
            'organisation__tenant_id',
            'organisation__tenant_name',
            'type',
            'grouping',
            'record_type'
        ]
        df_accounts_combined = df_accounts[common_columns + ['account_id', 'code', 'name']].copy()
        df_rollup_combined = df_rollup[common_columns + ['account_count']].copy()
        # Fill missing columns with None
        df_accounts_combined['account_count'] = None
        df_rollup_combined['account_id'] = None
        df_rollup_combined['code'] = None
        df_rollup_combined['name'] = None
        # Combine DataFrames
        df_combined = pd.concat([df_accounts_combined, df_rollup_combined], ignore_index=True)
        table_id_combined = f'Xero.AccountsWithRollups_{tenant_id.replace("-", "_")}'
        
        # Export all three tables in parallel using async
        exports = [
            (df_accounts, table_id_accounts),
            (df_rollup, table_id_rollup),
            (df_combined, table_id_combined),
        ]
        
        # Export all three tables in parallel using async (2-3x faster)
        try:
            run_async_export(update_google_big_query_batch_async(exports))
            logger.info(f"Exported {len(df_accounts)} accounts to BigQuery table {table_id_accounts}")
            logger.info(f"Exported {len(df_rollup)} account rollups to BigQuery table {table_id_rollup}")
            logger.info(f"Exported {len(df_combined)} records to combined BigQuery table {table_id_combined}")
        except Exception as e:
            # Fallback to sequential sync exports if async fails
            logger.warning(f"Async batch export failed, using sync: {str(e)}")
            update_google_big_query(df_accounts, table_id_accounts)
            logger.info(f"Exported {len(df_accounts)} accounts to BigQuery table {table_id_accounts}")
            update_google_big_query(df_rollup, table_id_rollup)
            logger.info(f"Exported {len(df_rollup)} account rollups to BigQuery table {table_id_rollup}")
            update_google_big_query(df_combined, table_id_combined)
            logger.info(f"Exported {len(df_combined)} records to combined BigQuery table {table_id_combined}")

    except Exception as e:
        logger.error(f"Failed to process account rollups for tenant {tenant_id}: {str(e)}")
        # Continue with raw accounts export
        update_google_big_query(df_accounts, table_id_accounts)

