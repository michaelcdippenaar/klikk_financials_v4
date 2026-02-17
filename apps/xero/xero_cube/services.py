"""
Xero cube services - data processing and consolidation.
"""
import datetime
import time
import logging
import pandas as pd
from decimal import Decimal
from django.db.models import Q, Sum, F
from django.db.models.functions import Coalesce

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import XeroJournals, Month, Year
from apps.xero.xero_cube.models import XeroTrailBalance, XeroBalanceSheet

logger = logging.getLogger(__name__)


def process_journals(tenant_id):
    """Process journals from source."""
    print('[PROCESS JOURNALS] Start Processing Journals from XeroJournalsSource')
    logger.info(f'Start Processing Journals for tenant {tenant_id}')
    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    from apps.xero.xero_data.models import XeroJournalsSource
    result = XeroJournalsSource.objects.create_journals_from_xero(organisation)
    print(f'[PROCESS JOURNALS] Journals processing complete')
    logger.info(f'Journals processing complete for tenant {tenant_id}')


def create_trail_balance(tenant_id, incremental=False, rebuild=False, exclude_manual_journals=False):
    """
    Create trail balance from journals.
    
    Args:
        tenant_id: Xero tenant ID
        incremental: If True, only process journals updated since last run
        rebuild: If True, force full rebuild and ignore existing data (overrides incremental)
        exclude_manual_journals: If True, only build trail balance from regular journals (exclude manual journals)
    """
    from apps.xero.xero_sync.models import XeroLastUpdate
    
    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    
    # If rebuild is True, force full rebuild regardless of incremental setting
    last_update_date = None
    if rebuild:
        logger.info("Rebuild mode: forcing full rebuild and ignoring existing data")
        print(f"[TRAIL BALANCE] REBUILD mode: forcing full rebuild, ignoring existing data")
        if exclude_manual_journals:
            print(f"[TRAIL BALANCE] REBUILD mode: excluding manual journals - only using regular journals")
        incremental = False
    # Get last update date for incremental updates
    elif incremental:
        try:
            last_update = XeroLastUpdate.objects.get(
                end_point='journals',
                organisation=organisation
            )
            if last_update.date:
                last_update_date = last_update.date
                logger.info(f"Using incremental update from {last_update_date}")
                print(f"[TRAIL BALANCE] Using incremental update from {last_update_date}")
        except XeroLastUpdate.DoesNotExist:
            logger.info("No previous update found, doing full rebuild")
            print(f"[TRAIL BALANCE] No previous update found, doing full rebuild")
            last_update_date = None
    
    # Get account balances - filter by date if incremental
    if last_update_date:
        # For incremental updates, we need to:
        # 1. First, get the affected periods (year/month) from new journals
        # 2. Then get ALL journals for those periods (not just new ones) to recalculate totals correctly
        logger.info(f"Incremental update mode: identifying affected periods since {last_update_date}")
        print(f"[TRAIL BALANCE] Incremental update mode: identifying affected periods since {last_update_date}")
        
        # Step 1: Get new journals to identify affected periods
        new_journals_filter = XeroJournals.objects.filter(
            organisation=organisation,
            date__gte=last_update_date
        )
        # Exclude manual journals if requested
        if exclude_manual_journals:
            new_journals_filter = new_journals_filter.exclude(journal_type='manual_journal')
        
        new_journals = new_journals_filter.annotate(
            month=Month('date'),
            year=Year('date')
        ).values('year', 'month').distinct()
        
        affected_periods = list(new_journals)
        logger.info(f"Affected periods: {affected_periods}")
        print(f"[TRAIL BALANCE] Found {len(affected_periods)} affected periods: {affected_periods}")
        
        if affected_periods:
            # Step 2: Get ALL journals for affected periods (not just new ones)
            # This ensures we recalculate totals correctly
            period_filters = Q()
            for period in affected_periods:
                period_filters |= Q(
                    date__year=period['year'],
                    date__month=period['month']
                )
            
            # Get all journals for affected periods
            qs = XeroJournals.objects.filter(
                organisation=organisation
            ).filter(period_filters)
            # Exclude manual journals if requested
            if exclude_manual_journals:
                qs = qs.exclude(journal_type='manual_journal')
            
            qs = qs.annotate(
                month=Month('date'),
                year=Year('date'),
                contact_id_value=Coalesce(F('contact_id'), F('transaction_source__contact_id')),
            ).values("account", "year", "month", "contact_id_value", "tracking1", "tracking2").order_by().annotate(
                amount=Sum("amount"),
            )
            logger.info(f"Incremental update: recalculating {len(affected_periods)} affected periods with all journals")
            print(f"[TRAIL BALANCE] Recalculating {len(affected_periods)} affected periods, found {qs.count()} journal aggregates")
        else:
            logger.warning("No affected periods found in incremental mode, but continuing with full rebuild")
            print(f"[TRAIL BALANCE] WARNING: No affected periods found, falling back to full rebuild")
            # Fall back to full rebuild if no affected periods
            qs = XeroJournals.objects.get_account_balances(organisation, exclude_manual_journals=exclude_manual_journals)
            last_update_date = None  # Clear last_update_date to trigger full rebuild in consolidate_journals
    else:
        # Get all balances for full rebuild
        logger.info("Full rebuild mode: getting all account balances")
        print(f"[TRAIL BALANCE] Full rebuild mode: getting all account balances")
        if exclude_manual_journals:
            print(f"[TRAIL BALANCE] Excluding manual journals - only using regular journals")
        qs = XeroJournals.objects.get_account_balances(organisation, exclude_manual_journals=exclude_manual_journals)
        print(f"[TRAIL BALANCE] Found {qs.count()} journal aggregates for full rebuild")
    
    print(f'[TRAIL BALANCE] Start Consolidate Journal Process - {qs.count()} journal aggregates to process')
    logger.info(f"Consolidating {qs.count()} journal aggregates into trail balance")
    
    # Convert queryset to list to ensure we can iterate multiple times
    journals_list = list(qs)
    print(f'[TRAIL BALANCE] Converted to list: {len(journals_list)} items')
    
    # Track Trail Balance creation
    from apps.xero.xero_sync.models import XeroLastUpdate
    
    try:
        result = XeroTrailBalance.objects.consolidate_journals(organisation, journals_list, last_update_date=last_update_date)
        print(f'[TRAIL BALANCE] Consolidation complete, checking created records...')
        
        tb = XeroTrailBalance.objects.filter(organisation=organisation).select_related(
            'account', 'account__business_unit', 'contact', 'tracking1', 'tracking2', 'organisation'
        )
        tb_count = tb.count()
        
        # Check for errors - if consolidation returned empty or count is 0, don't update timestamp
        if tb_count == 0:
            error_msg = "Trail Balance creation resulted in 0 records"
            logger.error(error_msg)
            print(f'[TRAIL BALANCE] ERROR: {error_msg}')
            # Don't update timestamp on error - preserve last successful date
        else:
            # Success - update timestamp
            XeroLastUpdate.objects.update_or_create_timestamp('trail_balance', organisation)
            print(f'[TRAIL BALANCE] Successfully created {tb_count} records')
    except Exception as e:
        error_msg = f"Trail Balance creation failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        print(f'[TRAIL BALANCE] ERROR: {error_msg}')
        # Don't update timestamp on error - preserve last successful date
        raise
    print(f'[TRAIL BALANCE] Total trail balance records after consolidation: {tb_count}')
    logger.info(f"Trail balance consolidation complete: {tb_count} total records")
    
    print('Start Trail Balance - Google Export')
    
    df = tb.to_dataframe([
        'organisation__tenant_id', 'organisation__tenant_name',
        'year', 'month', 'fin_year', 'fin_period',
        'account__account_id',
        'account__type',
        'account__grouping',
        'account__code',
        'account__name',
        'account__business_unit__business_unit_code',
        'account__business_unit__business_unit_description',
        'account__business_unit__division_code',
        'account__business_unit__division_description',
        'contact__name',
        'contact__contacts_id',
        'tracking1__option',
        'tracking2__option',
        'amount',
        'balance_to_date'
    ])

    print('Trail Balance - DataFrame Created')

    # Filter zero amounts and convert to numeric types for BigQuery
    df = df[df.amount != 0].copy()
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
    df['fin_period'] = pd.to_numeric(df['fin_period'], errors='coerce')
    # Convert balance_to_date to numeric (may be NaN for non-P&L accounts)
    df['balance_to_date'] = pd.to_numeric(df['balance_to_date'], errors='coerce')
    table_id = f'Xero.TrailBalance_Movement_V2_{tenant_id.replace("-", "_")}'
    
    # Export to BigQuery (optional; skip if credentials not configured)
    from apps.xero.xero_integration.services import update_google_big_query, run_async_export, update_google_big_query_async
    try:
        run_async_export(update_google_big_query_async(df, table_id))
        print('End Trail Balance - Google Export')
    except Exception as e:
        try:
            logger.warning(f"Async export failed, using sync: {str(e)}")
            update_google_big_query(df, table_id)
            print('End Trail Balance - Google Export')
        except Exception as e2:
            logger.warning(f"BigQuery export skipped (trail balance still created): {e2}")
            print(f"BigQuery export skipped: {e2}")


def calculate_profit_loss_balance_to_date(tenant_id):
    """
    Calculate balance_to_date (YTD) for Profit & Loss accounts.
    
    For P&L accounts (REVENUE and EXPENSE), calculates the cumulative sum
    of all previous months up to and including the current month.
    This is done after processing the cube.
    
    Args:
        tenant_id: Xero tenant ID
    """
    from apps.xero.xero_cube.models import XeroTrailBalance
    
    logger.info(f'Start calculating P&L balance_to_date for tenant {tenant_id}')
    print(f"[P&L YTD] Starting balance_to_date calculation for tenant {tenant_id}")
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    # P&L account types
    pnl_account_types = ['REVENUE', 'EXPENSE']
    
    # Get all P&L accounts for this organisation
    pnl_accounts = XeroTrailBalance.objects.filter(
        organisation=organisation,
        account__type__in=pnl_account_types
    ).select_related('account', 'contact', 'tracking1', 'tracking2').order_by(
        'account', 'contact', 'tracking1', 'tracking2', 'year', 'month'
    )
    
    if not pnl_accounts.exists():
        logger.info(f"No P&L accounts found for tenant {tenant_id}")
        print(f"[P&L YTD] No P&L accounts found")
        return
    
    # Group by account, contact, tracking1, tracking2 to calculate YTD for each combination
    # Balance to date = total of all previous months (cumulative from the start)
    # Get distinct combinations
    distinct_combinations = pnl_accounts.values(
        'account', 'contact', 'tracking1', 'tracking2'
    ).distinct()
    
    total_updated = 0
    batch_size = 1000
    to_update = []
    
    print(f"[P&L YTD] Processing {distinct_combinations.count()} account/contact/tracking combinations")
    logger.info(f"Processing {distinct_combinations.count()} account/contact/tracking combinations")
    
    for combo in distinct_combinations:
        # Get all records for this combination, ordered by year and month
        # This ensures we calculate cumulative balance correctly across all periods
        records = pnl_accounts.filter(
            account=combo['account'],
            contact=combo['contact'],
            tracking1=combo['tracking1'],
            tracking2=combo['tracking2']
        ).order_by('year', 'month')
        
        # Calculate cumulative balance (balance_to_date) for each record
        # Balance to date = sum of all previous months up to and including current month
        cumulative_balance = Decimal('0')
        
        for record in records:
            # Add current month's amount to cumulative balance
            cumulative_balance += record.amount
            
            # Update balance_to_date if it's different
            if record.balance_to_date != cumulative_balance:
                record.balance_to_date = cumulative_balance
                to_update.append(record)
                
                # Batch update when we reach batch_size
                if len(to_update) >= batch_size:
                    XeroTrailBalance.objects.bulk_update(
                        to_update,
                        ['balance_to_date'],
                        batch_size=batch_size
                    )
                    total_updated += len(to_update)
                    print(f"[P&L YTD] Updated {total_updated} records...")
                    to_update = []
    
    # Update remaining records
    if to_update:
        XeroTrailBalance.objects.bulk_update(
            to_update,
            ['balance_to_date'],
            batch_size=batch_size
        )
        total_updated += len(to_update)
    
    logger.info(f"Completed balance_to_date calculation: updated {total_updated} P&L records")
    print(f"[P&L YTD] ✓ Completed: updated {total_updated} P&L records")


def create_balance_sheet(tenant_id):
    """Create balance sheet from trail balance."""
    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    XeroBalanceSheet.objects.consolidate_balance_sheet(organisation)
    tb = XeroBalanceSheet.objects.filter(organisation=organisation).select_related(
        'account', 'account__business_unit', 'contact', 'organisation'
    )
    df = tb.to_dataframe([
        'organisation__tenant_id', 'organisation__tenant_name', 'year', 'month',
        'account__account_id', 'account__type', 'account__business_unit__division_code',
        'account__business_unit__division_description', 'account__business_unit__business_unit_code',
        'account__business_unit__business_unit_description', 'account__grouping', 'account__code',
        'account__name', 'contact__name', 'amount', 'balance'
    ])
    df['amount'] = pd.to_numeric(df['amount'])
    df['balance'] = pd.to_numeric(df['balance'])
    table_id = f'Xero.BalanceSheet_Balance_{tenant_id.replace("-", "_")}'
    
    # Export to BigQuery (optional; skip if credentials not configured)
    from apps.xero.xero_integration.services import update_google_big_query, run_async_export, update_google_big_query_async
    try:
        run_async_export(update_google_big_query_async(df, table_id))
    except Exception as e:
        try:
            logger.warning(f"Async export failed, using sync: {str(e)}")
            update_google_big_query(df, table_id)
        except Exception as e2:
            logger.warning(f"BigQuery export skipped for balance sheet: {e2}")


def process_xero_data(tenant_id, rebuild_trail_balance=False, exclude_manual_journals=False):
    """
    Service function to process Xero data (trail balance, etc.).
    Extracted from XeroProcessDataView for use in scheduled tasks.
    
    Processing order:
    1. Process journals from XeroJournalsSource to XeroJournals
    2. Create trail balance from processed journals
    3. Calculate balance_to_date for P&L accounts
    
    Note: Metadata and Data Source updates must complete before this runs.
    
    Args:
        tenant_id: Xero tenant ID
        rebuild_trail_balance: If True, force full rebuild of trail balance and ignore existing data
        exclude_manual_journals: If True, only build trail balance from regular journals (exclude manual journals)
    
    Returns:
        dict: Result with status, message, and stats
    """
    start_time = time.time()
    
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")
    
    stats = {
        'journals_processed': False,
        'trail_balance_created': False,
        'pnl_balance_to_date_calculated': False,
        'balance_sheet_created': False,
        'accounts_exported': False,
    }
    
    try:
        # Step 1: Process journals from XeroJournalsSource to XeroJournals
        logger.info(f'Start Processing Journals for tenant {tenant_id}')
        print(f"[PROCESS] Starting journal processing for tenant {tenant_id}")
        process_journals(tenant_id)
        stats['journals_processed'] = True
        print(f"[PROCESS] ✓ Journals processed")
        logger.info(f'Journals processed for tenant {tenant_id}')
        
        # Step 2: Create trail balance from processed journals
        logger.info(f'Start Creating Trail Balance for tenant {tenant_id}')
        print(f"[PROCESS] Starting trail balance creation for tenant {tenant_id}")
        if rebuild_trail_balance:
            print(f"[PROCESS] REBUILD mode: forcing full rebuild of trail balance")
        if exclude_manual_journals:
            print(f"[PROCESS] Excluding manual journals - only using regular journals for trail balance")
        create_trail_balance(tenant_id, incremental=not rebuild_trail_balance, rebuild=rebuild_trail_balance, exclude_manual_journals=exclude_manual_journals)
        stats['trail_balance_created'] = True
        print(f"[PROCESS] ✓ Trail balance created")
        
        # Step 3: Calculate balance_to_date for P&L accounts
        logger.info(f'Start calculating P&L balance_to_date for tenant {tenant_id}')
        print(f"[PROCESS] Starting P&L balance_to_date calculation for tenant {tenant_id}")
        calculate_profit_loss_balance_to_date(tenant_id)
        stats['pnl_balance_to_date_calculated'] = True
        print(f"[PROCESS] ✓ P&L balance_to_date calculated")
        
        # Uncomment if needed
        # create_balance_sheet(tenant_id)
        # stats['balance_sheet_created'] = True
        
        # Uncomment if needed
        # from apps.xero.xero_integration.services import export_accounts
        # export_accounts(tenant_id)
        # stats['accounts_exported'] = True
        
        duration = time.time() - start_time
        stats['duration_seconds'] = duration
        
        return {
            'success': True,
            'message': f"Data processed for tenant {tenant_id}",
            'stats': stats
        }
        
    except Exception as e:
        duration = time.time() - start_time
        error_msg = f"Failed to process data for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)


def import_pnl_by_tracking(tenant_id, from_date=None, to_date=None, periods=11, user=None):
    """
    Pull Xero Profit & Loss report for each tracking category option and store
    per-account/month values in XeroPnlByTracking.

    1. Fetch tracking categories from Xero to get category UUIDs + option UUIDs.
    2. For each tracking option, call the P&L API with tracking filter.
    3. Parse monthly amounts per account and store in XeroPnlByTracking.

    Args:
        tenant_id: Xero tenant ID
        from_date: Start date (date or 'YYYY-MM-DD' string). Defaults to 12 months ago.
        to_date: End date (date or 'YYYY-MM-DD' string). Defaults to today.
        periods: Number of comparison periods (default 11 = 12 months)
        user: User for API auth (optional, falls back to active credentials)

    Returns:
        dict with summary stats
    """
    from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi, serialize_model
    from apps.xero.xero_metadata.models import XeroAccount, XeroTracking
    from apps.xero.xero_cube.models import XeroPnlByTracking
    from datetime import date as date_cls, timedelta
    from decimal import Decimal, InvalidOperation

    start_time = time.time()
    organisation = XeroTenant.objects.get(tenant_id=tenant_id)

    # Default date range: last 12 months (must stay within 365 days for Xero API)
    if to_date is None:
        to_date = date_cls.today()
    elif isinstance(to_date, str):
        to_date = datetime.datetime.strptime(to_date, '%Y-%m-%d').date()
    if from_date is None:
        # Go back 11 months from the 1st of the current month to stay within 365 days
        m = to_date.month - 11
        y = to_date.year
        while m <= 0:
            m += 12
            y -= 1
        from_date = date_cls(y, m, 1)
    elif isinstance(from_date, str):
        from_date = datetime.datetime.strptime(from_date, '%Y-%m-%d').date()

    # Validate: Xero requires fromDate and toDate within 365 days
    if (to_date - from_date).days > 365:
        raise ValueError(f"Date range {from_date} to {to_date} exceeds 365 days. "
                         f"Xero P&L API requires dates within 365 days of each other.")

    # Resolve user
    if not user:
        from apps.xero.xero_auth.models import XeroClientCredentials
        creds = XeroClientCredentials.objects.filter(active=True).first()
        if not creds:
            raise ValueError("No active Xero credentials found and no user provided")
        user = creds.user

    # Init API client
    api_client = XeroApiClient(user, tenant_id=tenant_id)
    xero_api = XeroAccountingApi(api_client, tenant_id)

    # ------------------------------------------------------------------
    # 1. Fetch tracking categories from Xero to get category UUIDs
    # ------------------------------------------------------------------
    print(f"[PNL-TRACKING] Fetching tracking categories from Xero ...")
    raw_tc = serialize_model(
        xero_api.api_client.get_tracking_categories(tenant_id, include_archived='True')
    ).get('TrackingCategories', [])

    # Build mapping: { tracking_option_id_uuid: (category_uuid, category_name, option_name) }
    option_map = {}  # option_uuid -> (category_uuid, category_name, option_name)
    for tc in raw_tc:
        cat_uuid = tc.get('TrackingCategoryID')
        cat_name = tc.get('Name', '')
        for opt in tc.get('Options', []):
            opt_uuid = opt.get('TrackingOptionID')
            opt_name = opt.get('Name', '')
            if opt_uuid:
                option_map[opt_uuid] = (cat_uuid, cat_name, opt_name)

    print(f"[PNL-TRACKING] Found {len(raw_tc)} tracking categories, {len(option_map)} options total")

    # Map our DB tracking records to their Xero UUIDs
    # IMPORTANT: Only use 'Profit Center' tracking category to avoid cross-category
    # double-counting. When Xero filters P&L by a tracking option from one category,
    # it returns totals across ALL options in other categories. Summing across
    # categories inflates numbers by N× (where N = number of categories).
    db_trackings = list(
        XeroTracking.objects.filter(organisation=organisation, name='Profit Center').order_by('name', 'option')
    )
    if not db_trackings:
        # Fallback: if no 'Profit Center' found, use all (original behaviour)
        print("[PNL-TRACKING] WARNING: No 'Profit Center' tracking found, using all tracking options")
        db_trackings = list(
            XeroTracking.objects.filter(organisation=organisation).order_by('name', 'option')
        )
    # Build account lookup by UUID
    accounts_by_uuid = {
        a.account_id: a for a in XeroAccount.objects.filter(organisation=organisation)
    }

    # ------------------------------------------------------------------
    # Build the list of months we want and compute the right API parameters.
    #
    # IMPORTANT: Xero P&L API with periods + timeframe=MONTH creates
    # rolling windows equal to the from_date→to_date span. To get
    # individual calendar months, we must use a SINGLE-MONTH date range
    # as the "anchor" and use periods to go back.
    #
    # Strategy:
    #   - Use a month with 31 days close to to_date as the anchor
    #     so comparison periods align to true month-ends.
    #   - If to_date's month has <31 days (e.g. Feb/Apr/Jun/Sep/Nov),
    #     find the nearest prior 31-day month, use that as anchor,
    #     then make extra calls for the remaining months.
    # ------------------------------------------------------------------
    import calendar

    # Build the full list of desired months
    desired_months = []
    cur = from_date
    while (cur.year, cur.month) <= (to_date.year, to_date.month):
        desired_months.append((cur.year, cur.month))
        if cur.month == 12:
            cur = date_cls(cur.year + 1, 1, 1)
        else:
            cur = date_cls(cur.year, cur.month + 1, 1)

    # Build API call plan: group months into batches that a single API call can cover.
    # Each API call can return up to 12 months (periods=11 + 1 main).
    # Use a 31-day month as anchor for proper calendar alignment.
    #
    # CRITICAL: The batch must include ALL months the API response will contain
    # (main period + all comparison periods), even if some are already covered
    # by another call. Duplicate inserts are handled by ignore_conflicts=True.
    api_call_plans = []  # list of (anchor_from, anchor_to, periods, batch_months)

    remaining = set(desired_months)

    # Strategy: find the best 31-day anchor near the end of the range,
    # then cover as many months as possible going back.
    while remaining:
        remaining_sorted = sorted(remaining)

        # Find the latest 31-day month in remaining
        anchor_ym = None
        for ym in reversed(remaining_sorted):
            _, days = calendar.monthrange(ym[0], ym[1])
            if days == 31:
                anchor_ym = ym
                break

        if not anchor_ym:
            # No 31-day month left (e.g. only Feb/Apr/Jun/Sep/Nov).
            # Use the latest remaining month as anchor.
            anchor_ym = remaining_sorted[-1]

        ay, am = anchor_ym
        _, anchor_days = calendar.monthrange(ay, am)
        anchor_from = f'{ay}-{am:02d}-01'
        anchor_to = f'{ay}-{am:02d}-{anchor_days:02d}'

        # The API response will include the main period + n comparison periods.
        # Each comparison period goes back 1 month from the previous.
        # We need to know which months the API will return:
        # Main = anchor month, then anchor-1, anchor-2, ..., anchor-n
        # Max periods = 11 (giving 12 total columns).

        # How many periods do we need? Enough to cover remaining months
        # at or before the anchor.
        months_at_or_before = [ym for ym in remaining_sorted if ym <= anchor_ym]
        n_periods = min(len(months_at_or_before) - 1, 11)
        n_periods = max(n_periods, 1)  # API requires periods >= 1

        # Build the batch: the months the API will actually return.
        # Start from anchor and go back n_periods months.
        batch = []
        y, m = ay, am
        for _ in range(n_periods + 1):
            batch.append((y, m))
            m -= 1
            if m < 1:
                m = 12
                y -= 1
        batch.reverse()  # chronological order (oldest first)

        api_call_plans.append((anchor_from, anchor_to, n_periods, batch))

        # Mark only the months we actually NEED as covered
        for ym in batch:
            remaining.discard(ym)

    # For backward compat, keep a master period_months list
    period_months = desired_months[:]

    print(f"[PNL-TRACKING] Months to import: {len(desired_months)} ({desired_months[0][0]}-{desired_months[0][1]:02d} to {desired_months[-1][0]}-{desired_months[-1][1]:02d})")
    print(f"[PNL-TRACKING] API call plan: {len(api_call_plans)} call(s)")

    def safe_decimal(val):
        if val in (None, '', 0, '0'):
            return Decimal('0')
        try:
            return Decimal(str(val))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal('0')

    # ------------------------------------------------------------------
    # 2. Helper to parse a P&L response and extract per-account values
    # ------------------------------------------------------------------
    def parse_pnl_response(pnl_data, batch_months, tracking_obj):
        """
        Parse a Xero P&L API response and return list of XeroPnlByTracking
        objects. batch_months is a list of (year, month) in chronological order
        (oldest first), matching the columns in reverse (newest first in API).
        """
        records = []
        reports = (pnl_data or {}).get('Reports', [])
        if not reports:
            return records
        report = reports[0]
        rows = report.get('Rows', [])

        def walk(row_list):
            for row in row_list:
                rt = row.get('RowType', '')
                if rt == 'Section':
                    walk(row.get('Rows', []))
                    continue
                if rt == 'Header':
                    continue
                nested = row.get('Rows')
                if nested:
                    walk(nested)
                if rt in ('Row', 'SummaryRow'):
                    cells = row.get('Cells', [])
                    if not cells:
                        continue
                    first = cells[0]
                    acct_uuid = None
                    for attr in first.get('Attributes', []):
                        if attr.get('Id') == 'account':
                            acct_uuid = attr.get('Value')
                    if not acct_uuid:
                        continue
                    account = accounts_by_uuid.get(acct_uuid)
                    if not account:
                        continue
                    period_cells = cells[1:]
                    n = len(period_cells)
                    for cell_idx, cell in enumerate(period_cells):
                        period_idx = n - 1 - cell_idx  # oldest=0
                        if period_idx < len(batch_months):
                            yr, mo = batch_months[period_idx]
                        else:
                            continue
                        val = safe_decimal(cell.get('Value', ''))
                        if val != Decimal('0'):
                            records.append(XeroPnlByTracking(
                                organisation=organisation,
                                tracking=tracking_obj,
                                account=account,
                                year=yr,
                                month=mo,
                                xero_amount=val,
                            ))
        walk(rows)
        return records

    def fetch_pnl_for_plan(label, tracking_category_id=None, tracking_option_id=None, tracking_obj=None):
        """
        Execute all API calls from the plan for a given target (tracking option or overall).
        Deduplicates records across API calls to handle overlapping months.
        Returns total records created.
        """
        all_records = []
        for anchor_from, anchor_to, n_periods, batch in api_call_plans:
            try:
                kwargs = dict(
                    from_date=anchor_from,
                    to_date=anchor_to,
                    periods=n_periods,
                    timeframe='MONTH',
                )
                if tracking_category_id:
                    kwargs['tracking_category_id'] = tracking_category_id
                    kwargs['tracking_option_id'] = tracking_option_id
                pnl_data = xero_api.profit_and_loss().get(**kwargs)
                stats['api_calls'] += 1
            except Exception as e:
                msg = f"API error for {label} ({anchor_from}): {e}"
                print(f"[PNL-TRACKING] ERROR: {msg}")
                stats['errors'].append(msg)
                continue

            records = parse_pnl_response(pnl_data, batch, tracking_obj)
            all_records.extend(records)

        # Deduplicate: keep first occurrence per (account, year, month)
        seen = set()
        unique_records = []
        for r in all_records:
            key = (r.account_id, r.year, r.month)
            if key not in seen:
                seen.add(key)
                unique_records.append(r)

        if unique_records:
            XeroPnlByTracking.objects.bulk_create(unique_records, ignore_conflicts=True)
            stats['records_created'] += len(unique_records)
        return len(unique_records)

    # ------------------------------------------------------------------
    # 3. Delete old data and run the import
    # ------------------------------------------------------------------
    stats = {
        'tracking_options_processed': 0,
        'records_created': 0,
        'api_calls': 0,
        'errors': [],
    }

    # Delete old data for this org
    XeroPnlByTracking.objects.filter(
        organisation=organisation,
    ).delete()

    # ------------------------------------------------------------------
    # 3a. Pull P&L for each tracking option
    # ------------------------------------------------------------------
    for trk in db_trackings:
        opt_uuid = trk.option_id
        info = option_map.get(opt_uuid)
        if not info:
            print(f"[PNL-TRACKING] SKIP {trk.option} — option UUID {opt_uuid} not found in Xero categories")
            continue
        cat_uuid, cat_name, opt_name = info

        print(f"[PNL-TRACKING] Pulling P&L for [{cat_name}] {opt_name} ...")
        n = fetch_pnl_for_plan(
            label=f"[{cat_name}] {opt_name}",
            tracking_category_id=cat_uuid,
            tracking_option_id=opt_uuid,
            tracking_obj=trk,
        )
        if n:
            print(f"[PNL-TRACKING]   Stored {n} records for {opt_name}")
        else:
            print(f"[PNL-TRACKING]   No non-zero P&L data for {opt_name}")
        stats['tracking_options_processed'] += 1

    # ------------------------------------------------------------------
    # 3b. Fetch OVERALL (unfiltered) P&L — tracking=NULL
    # ------------------------------------------------------------------
    print(f"[PNL-TRACKING] Pulling OVERALL P&L (no tracking filter) ...")
    n = fetch_pnl_for_plan(label="OVERALL", tracking_obj=None)
    if n:
        print(f"[PNL-TRACKING]   Stored {n} OVERALL (unfiltered) records")
    else:
        print(f"[PNL-TRACKING]   No non-zero overall P&L data")

    duration = time.time() - start_time
    stats['duration_seconds'] = round(duration, 1)
    print(f"[PNL-TRACKING] Done: {stats['tracking_options_processed']} options, "
          f"{stats['records_created']} records, {stats['api_calls']} API calls in {stats['duration_seconds']}s")
    if stats['errors']:
        print(f"[PNL-TRACKING] Errors: {stats['errors']}")

    return {
        'success': True,
        'message': (f"Imported P&L by tracking: {stats['tracking_options_processed']} options, "
                    f"{stats['records_created']} records"),
        'stats': stats,
    }


def process_profit_loss(tenant_id, user=None):
    """
    Process Profit & Loss reports - import and validate.
    
    This runs after process_xero_data completes.
    
    Args:
        tenant_id: Xero tenant ID
        user: User object for API authentication (optional)
    
    Returns:
        dict: Result with status, message, and stats
    """
    from apps.xero.xero_validation.services.imports import import_profit_loss_from_xero
    from apps.xero.xero_validation.services.profit_loss_validation import validate_profit_loss_with_fallback
    from apps.xero.xero_sync.models import XeroLastUpdate
    from datetime import date, timedelta
    
    start_time = time.time()
    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    
    stats = {
        'pnl_imported': False,
        'pnl_validated': False,
        'in_sync': True,
    }
    
    try:
        # Calculate date range for P&L report (last 12 months)
        to_date = date.today()
        from_date = to_date - timedelta(days=365)  # Approximately 12 months
        
        # Import P&L report
        logger.info(f'Starting P&L import for tenant {tenant_id}')
        print(f"[P&L] Starting P&L import for tenant {tenant_id}")
        import_result = import_profit_loss_from_xero(
            tenant_id=tenant_id,
            from_date=from_date,
            to_date=to_date,
            periods=11,  # 12 months (0-11)
            timeframe='MONTH',
            user=user
        )
        
        if import_result.get('success'):
            stats['pnl_imported'] = True
            print(f"[P&L] ✓ P&L imported successfully")
            logger.info(f'P&L imported for tenant {tenant_id}')
            # Update timestamp immediately after API call succeeds, before validation
            XeroLastUpdate.objects.update_or_create_timestamp('profit_loss', organisation)
        else:
            raise Exception(f"P&L import failed: {import_result.get('message', 'Unknown error')}")
        
        # Validate P&L (with fallback to previous month)
        logger.info(f'Starting P&L validation for tenant {tenant_id}')
        print(f"[P&L] Starting P&L validation for tenant {tenant_id}")
        validation_result = validate_profit_loss_with_fallback(tenant_id)
        
        stats['pnl_validated'] = True
        stats['in_sync'] = validation_result.get('in_sync', False)
        stats['validation_errors'] = len(validation_result.get('errors', []))
        
        if validation_result.get('in_sync'):
            print(f"[P&L] ✓ P&L validation passed")
            logger.info(f'P&L validation passed for tenant {tenant_id}')
        else:
            print(f"[P&L] ✗ P&L validation failed: {len(validation_result.get('errors', []))} errors")
            logger.warning(f'P&L validation failed for tenant {tenant_id}: {validation_result.get("errors", [])[:3]}')
            # Don't update timestamp on validation failure - preserve last successful date
        
        duration = time.time() - start_time
        stats['duration_seconds'] = duration
        
        return {
            'success': True,
            'message': f"P&L processed for tenant {tenant_id}",
            'stats': stats,
            'validation_result': validation_result
        }
        
    except Exception as e:
        duration = time.time() - start_time
        error_msg = f"Failed to process P&L for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        
        # Don't update timestamp on error - preserve last successful date
        
        stats['duration_seconds'] = duration
        return {
            'success': False,
            'message': error_msg,
            'stats': stats
        }
