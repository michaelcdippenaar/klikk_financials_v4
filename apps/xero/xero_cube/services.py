"""
Xero cube services - data processing and consolidation.
"""
import datetime
import time
import logging
import pandas as pd
from decimal import Decimal
from django.db import models

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import XeroJournals, Month, Year
from apps.xero.xero_cube.models import XeroTrailBalance, XeroBalanceSheet

logger = logging.getLogger(__name__)


def process_journals(tenant_id, force_reprocess=False):
    """Process journals from source.
    
    Args:
        tenant_id: Xero tenant ID
        force_reprocess: If True, re-process all journal sources (including already processed)
                        to fix tracking assignment. Use when rebuilding trail balance after
                        metadata/tracking slot changes.
    """
    print('[PROCESS JOURNALS] Start Processing Journals from XeroJournalsSource')
    logger.info(f'Start Processing Journals for tenant {tenant_id}')
    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    from apps.xero.xero_data.models import XeroJournalsSource
    if not force_reprocess and not XeroJournalsSource.objects.filter(
        organisation=organisation,
        processed=False,
    ).exists():
        print('[PROCESS JOURNALS] No unprocessed source journals; skipping')
        logger.info(f'No unprocessed journals for tenant {tenant_id}; skipping')
        return {'skipped': True, 'processed': 0}
    result = XeroJournalsSource.objects.create_journals_from_xero(organisation, force_reprocess=force_reprocess)
    print(f'[PROCESS JOURNALS] Journals processing complete')
    logger.info(f'Journals processing complete for tenant {tenant_id}')
    return {'skipped': False, 'processed': result.count()}


def create_trail_balance(tenant_id, incremental=False, rebuild=False, exclude_manual_journals=False,
                         affected_periods=None):
    """
    Create trail balance from journals via a single SQL INSERT...SELECT.

    Args:
        tenant_id: Xero tenant ID
        incremental: If True, only process journals updated since last run
        rebuild: If True, force full rebuild and ignore existing data (overrides incremental)
        exclude_manual_journals: If True, only build trail balance from regular journals (exclude manual journals)
        affected_periods: Optional list of (year, month) tuples from the Xero fetch step.
            When provided, this is preferred over timestamp-based inference.
    """
    from apps.xero.xero_sync.models import XeroLastUpdate

    organisation = XeroTenant.objects.get(tenant_id=tenant_id)

    selected_periods = None  # None = full rebuild

    if rebuild:
        logger.info("Rebuild mode: forcing full rebuild")
        print(f"[TRAIL BALANCE] REBUILD mode: forcing full rebuild")
        incremental = False
    elif affected_periods is not None:
        selected_periods = [tuple(p) for p in affected_periods]
        if not selected_periods:
            tb_count = XeroTrailBalance.objects.filter(organisation=organisation).count()
            print("[TRAIL BALANCE] No affected periods from Xero sync; skipping rebuild")
            return {'skipped': True, 'records': tb_count, 'affected_periods': []}
        print(f"[TRAIL BALANCE] Using {len(selected_periods)} affected periods from sync: {selected_periods}")
    elif incremental:
        # Fallback only: process_xero_data() derives the affected periods from
        # what actually changed and passes them in. This branch is reached by
        # direct callers, and keys on the last trail-balance build (not the
        # legacy Journals-API cursor, frozen at 2025-11-25, which made every
        # "incremental" run rebuild ten months).
        try:
            last_update = XeroLastUpdate.objects.get(end_point='trail_balance', organisation=organisation)
            if last_update.date:
                last_update_date = last_update.date
                print(f"[TRAIL BALANCE] Incremental from {last_update_date}")

                new_journals_filter = XeroJournals.objects.filter(
                    organisation=organisation, date__gte=last_update_date
                )
                if exclude_manual_journals:
                    new_journals_filter = new_journals_filter.exclude(journal_type='manual_journal')

                # .order_by() is load-bearing: without it the model's default
                # ordering (organisation, date, journal_number) leaks into the
                # DISTINCT, which then yields one row per journal instead of
                # one per month — 2,822 "periods" for 12 months on 2026-09-03.
                periods_qs = new_journals_filter.annotate(
                    _month=Month('date'), _year=Year('date')
                ).values('_year', '_month').order_by().distinct()

                selected_periods = sorted({(int(p['_year']), int(p['_month'])) for p in periods_qs})
                print(f"[TRAIL BALANCE] {len(selected_periods)} affected periods: {selected_periods}")

                if not selected_periods:
                    logger.warning("No affected periods in incremental mode, falling back to full rebuild")
                    print(f"[TRAIL BALANCE] WARNING: no affected periods, falling back to full rebuild")
                    selected_periods = None
        except XeroLastUpdate.DoesNotExist:
            logger.info("No previous update found, doing full rebuild")
            print(f"[TRAIL BALANCE] No previous update, full rebuild")

    try:
        XeroTrailBalance.objects.consolidate_journals(
            organisation,
            exclude_manual_journals=exclude_manual_journals,
            affected_periods=selected_periods,
        )

        tb = XeroTrailBalance.objects.filter(organisation=organisation).select_related(
            'account', 'account__business_unit', 'contact', 'tracking1', 'tracking2', 'organisation'
        )
        tb_count = tb.count()

        if tb_count == 0:
            logger.error("Trail Balance creation resulted in 0 records")
            print(f'[TRAIL BALANCE] ERROR: 0 records created')
        else:
            XeroLastUpdate.objects.update_or_create_timestamp('trail_balance', organisation)
            print(f'[TRAIL BALANCE] ✓ {tb_count} records')
    except Exception as e:
        logger.error(f"Trail Balance creation failed: {e}", exc_info=True)
        print(f'[TRAIL BALANCE] ERROR: {e}')
        raise

    # BigQuery export. Check for credentials BEFORE loading the whole table
    # into a dataframe: without them the export is known to fail (not a
    # regression — see CLAUDE.md) and the ~120k-row dataframe was pure waste.
    from apps.xero.xero_integration.services import has_google_credentials
    if not has_google_credentials():
        print('BigQuery export skipped: no Google credentials configured')
        return {'skipped': False, 'records': tb_count, 'affected_periods': selected_periods}

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
        'debit',
        'credit',
        'balance_to_date'
    ])

    df = df[(df.amount != 0) | (df.debit != 0) | (df.credit != 0)].copy()
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
    df['debit'] = pd.to_numeric(df['debit'], errors='coerce')
    df['credit'] = pd.to_numeric(df['credit'], errors='coerce')
    df['fin_period'] = pd.to_numeric(df['fin_period'], errors='coerce')
    df['balance_to_date'] = pd.to_numeric(df['balance_to_date'], errors='coerce')
    table_id = f'Xero.TrailBalance_Movement_V2_{tenant_id.replace("-", "_")}'

    from apps.xero.xero_integration.services import update_google_big_query, run_async_export, update_google_big_query_async
    try:
        run_async_export(update_google_big_query_async(df, table_id))
        print('End Trail Balance - Google Export')
    except Exception as e:
        try:
            logger.warning(f"Async export failed, using sync: {e}")
            update_google_big_query(df, table_id)
            print('End Trail Balance - Google Export')
        except Exception as e2:
            logger.warning(f"BigQuery export skipped (trail balance still created): {e2}")
            print(f"BigQuery export skipped: {e2}")

    return {'skipped': False, 'records': tb_count, 'affected_periods': selected_periods}


def fill_balance_sheet_gaps(tenant_id, affected_periods=None):
    """
    Insert zero-amount gap-fill rows into XeroTrailBalance for balance sheet
    accounts so that every partition (account, contact, tracking1, tracking2)
    has a row for every month from its first movement to the account's latest
    month.  This is required so that the subsequent balance_to_date window
    function produces a value for every month, not just months with movement.

    Args:
        tenant_id: Xero tenant ID
        affected_periods: optional list of (year, month) just rebuilt. When
            given, only ACCOUNTS with a row in those months are examined —
            accounts, not partitions, because a new month on one partition
            extends the account's latest month and every sibling partition
            then needs a zero row for it; and the whole series of each such
            account, not just the affected months, because a backdated
            movement earlier than a partition's old first month needs the
            months in between filled too.

    Returns:
        int: number of gap-fill rows inserted
    """
    from django.db import connection

    organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    fiscal_start = organisation.get_fiscal_year_start_month()

    logger.info(f'Starting balance sheet gap-fill for tenant {tenant_id}')
    print(f"[BS GAP-FILL] Starting gap-fill for tenant {tenant_id}")

    account_filter = ""
    account_params = []
    if affected_periods:
        account_filter = """
              AND tb.account_id IN (
                  SELECT DISTINCT account_id FROM xero_cube_xerotrailbalance
                  WHERE organisation_id = %s
                    AND (year * 100 + month) = ANY(%s)
              )"""
        account_params = [tenant_id, [int(y) * 100 + int(m) for y, m in affected_periods]]

    # The NOT EXISTS below compares nullable keys with COALESCE instead of
    # IS NOT DISTINCT FROM: the latter is not hash- or merge-joinable, so the
    # planner rescanned the existing-rows side 15.1M times (1.9 s per run);
    # equality on COALESCE'd keys lets it hash anti-join. Sentinels cannot
    # collide: contact ids are Xero UUIDs (never ''), tracking ids are
    # positive bigints (never -1).
    sql = """
        INSERT INTO xero_cube_xerotrailbalance
            (organisation_id, account_id, date, year, month,
             fin_year, fin_period,
             contact_id, tracking1_id, tracking2_id,
             amount, debit, credit, tax_amount, balance_to_date)
        SELECT
            p.organisation_id,
            p.account_id,
            make_date(gs.yr, gs.mo, 1),
            gs.yr,
            gs.mo,
            CASE WHEN gs.mo >= %s THEN gs.yr ELSE gs.yr - 1 END,
            CASE WHEN gs.mo >= %s THEN gs.mo - %s + 1
                                  ELSE gs.mo + (12 - %s) + 1 END,
            p.contact_id,
            p.tracking1_id,
            p.tracking2_id,
            0,
            0,
            0,
            0,
            NULL
        FROM (
            SELECT
                tb.organisation_id,
                tb.account_id,
                tb.contact_id,
                tb.tracking1_id,
                tb.tracking2_id,
                MIN(make_date(tb.year, tb.month, 1)) AS min_date,
                MAX(make_date(acct_max.max_y, acct_max.max_m, 1)) AS max_date
            FROM xero_cube_xerotrailbalance tb
            JOIN xero_metadata_xeroaccount acc
                ON acc.account_id = tb.account_id
            JOIN (
                SELECT account_id,
                       MAX(year)  FILTER (WHERE year * 100 + month = max_ym) AS max_y,
                       MAX(month) FILTER (WHERE year * 100 + month = max_ym) AS max_m
                FROM (
                    SELECT account_id, year, month,
                           MAX(year * 100 + month) OVER (PARTITION BY account_id) AS max_ym
                    FROM xero_cube_xerotrailbalance
                    WHERE organisation_id = %s
                ) sub
                GROUP BY account_id
            ) acct_max ON acct_max.account_id = tb.account_id
            WHERE tb.organisation_id = %s
              AND acc.grouping IN ('ASSET', 'LIABILITY', 'EQUITY')
              {account_filter}
            GROUP BY tb.organisation_id, tb.account_id,
                     tb.contact_id, tb.tracking1_id, tb.tracking2_id
        ) p
        CROSS JOIN LATERAL generate_series(
            p.min_date, p.max_date, '1 month'::interval
        ) AS gs_date
        CROSS JOIN LATERAL (
            SELECT EXTRACT(YEAR  FROM gs_date)::int AS yr,
                   EXTRACT(MONTH FROM gs_date)::int AS mo
        ) gs
        WHERE NOT EXISTS (
            SELECT 1 FROM xero_cube_xerotrailbalance ex
            WHERE ex.organisation_id = p.organisation_id
              AND ex.account_id      = p.account_id
              AND COALESCE(ex.contact_id, '')    = COALESCE(p.contact_id, '')
              AND COALESCE(ex.tracking1_id, -1)  = COALESCE(p.tracking1_id, -1)
              AND COALESCE(ex.tracking2_id, -1)  = COALESCE(p.tracking2_id, -1)
              AND ex.year  = gs.yr
              AND ex.month = gs.mo
        )
    """.replace('{account_filter}', account_filter)

    params = [
        fiscal_start, fiscal_start, fiscal_start, fiscal_start,
        tenant_id, tenant_id, *account_params,
    ]

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        total_inserted = cursor.rowcount

    logger.info(f"Gap-fill complete: inserted {total_inserted} zero-amount rows")
    print(f"[BS GAP-FILL] ✓ Inserted {total_inserted} zero-amount rows")
    return total_inserted


def calculate_balance_sheet_balance_to_date(tenant_id, affected_periods=None):
    """
    Calculate balance_to_date (YTD) for balance sheet accounts (ASSET, LIABILITY, EQUITY).

    1. Fill monthly gaps so every partition has a row for every month.
    2. Run a single SQL UPDATE with a window function to set balance_to_date.

    Args:
        tenant_id: Xero tenant ID
        affected_periods: optional (year, month) list just rebuilt; narrows the
            gap-fill to the accounts touched (see fill_balance_sheet_gaps).
            The running-total UPDATE stays whole-tenant: it only writes rows
            whose value actually changes, and a rebuilt month shifts every
            later month's balance in its partition anyway.
    """
    from django.db import connection

    logger.info(f'Start calculating balance sheet balance_to_date for tenant {tenant_id}')
    print(f"[BS YTD] Starting balance_to_date calculation for tenant {tenant_id}")

    try:
        XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")

    fill_balance_sheet_gaps(tenant_id, affected_periods=affected_periods)

    sql = """
        UPDATE xero_cube_xerotrailbalance tb
        SET balance_to_date = sub.running_total
        FROM (
            SELECT tb_inner.id,
                   SUM(tb_inner.amount) OVER (
                       PARTITION BY tb_inner.account_id, tb_inner.contact_id,
                                    tb_inner.tracking1_id, tb_inner.tracking2_id
                       ORDER BY tb_inner.year, tb_inner.month
                   ) AS running_total
            FROM xero_cube_xerotrailbalance tb_inner
            WHERE tb_inner.organisation_id = %s
              AND tb_inner.account_id IN (
                  SELECT account_id FROM xero_metadata_xeroaccount
                  WHERE organisation_id = %s AND grouping IN ('ASSET', 'LIABILITY', 'EQUITY')
              )
        ) sub
        WHERE tb.id = sub.id
          AND tb.balance_to_date IS DISTINCT FROM sub.running_total
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, [tenant_id, tenant_id])
        total_updated = cursor.rowcount

    logger.info(f"Completed balance_to_date calculation: updated {total_updated} balance sheet records")
    print(f"[BS YTD] ✓ Completed: updated {total_updated} balance sheet records (single SQL window function)")


# Backward compatibility: old name now runs balance sheet YTD (no longer P&L)
calculate_profit_loss_balance_to_date = calculate_balance_sheet_balance_to_date


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


def _months_of(journals_qs):
    """Distinct (year, month) of a XeroJournals queryset, as a set of int tuples.

    `.order_by()` is required: the model's default ordering would otherwise
    join the DISTINCT and return one row per journal (see create_trail_balance).
    """
    rows = journals_qs.annotate(
        _month=Month('date'), _year=Year('date')
    ).values('_year', '_month').order_by().distinct()
    return {(int(r['_year']), int(r['_month'])) for r in rows}


# Above this share of transactions touched, a full reprocess + full rebuild is
# both cheaper (one set-based statement each) and simpler than the incremental
# bookkeeping. Also what the first run after the synced_at backfill hits.
_FULL_REBUILD_TOUCHED_RATIO = 0.5


def derive_incremental_scope(tenant):
    """Work out what changed since the last build, from the database alone.

    Returns None when a full rebuild is the right answer, else a dict:
        touched_transaction_ids: set of XeroTransactionSource.transactions_id
            written by a Xero sync after the last transaction reprocess
            (XeroLastUpdate['process_journals'] vs XeroTransactionSource.synced_at)
        pending_journal_source_ids: XeroJournalsSource rows awaiting processing
            (manual journals fetched or re-fetched since)
        affected_periods: (year, month) set the trail balance must rebuild —
            the months those rows' CURRENT journals sit in (the "before" side:
            a re-dated transaction must also clear its old month) plus the
            months of any exclusion rule changed since the last build. The
            caller adds the "after" months once the journals are regenerated.

    Why derive rather than thread: the console button and each V2 stage are
    separate requests, so the touched set the sync computed in-process is gone
    by the time the build runs. Before 2026-09-03 both entry points therefore
    reprocessed all ~45k transaction journals and rebuilt ten months on every
    run, and a backdated edit whose journals fell outside those months was
    never rebuilt at all until someone forced rebuild=True.
    """
    import re as _re
    from django.db.models import Q
    from apps.xero.xero_data.models import (
        XeroTransactionSource, XeroJournalsSource, XeroJournalExclusion,
    )
    from apps.xero.xero_sync.models import XeroLastUpdate

    stamps = dict(XeroLastUpdate.objects.filter(
        organisation=tenant, end_point__in=('process_journals', 'trail_balance'),
    ).values_list('end_point', 'date'))
    last_reprocess = stamps.get('process_journals')
    last_build = stamps.get('trail_balance')
    if not last_reprocess or not last_build:
        print("[SCOPE] No previous incremental build stamp; full rebuild")
        return None

    sources = XeroTransactionSource.objects.filter(organisation=tenant)
    total = sources.count()
    touched = set(sources.filter(synced_at__gt=last_reprocess)
                  .values_list('transactions_id', flat=True))
    if total and len(touched) >= total * _FULL_REBUILD_TOUCHED_RATIO:
        print(f"[SCOPE] {len(touched)}/{total} transactions touched since "
              f"{last_reprocess:%Y-%m-%d %H:%M}; full rebuild is cheaper")
        return None

    periods = set()
    if touched:
        periods |= _months_of(XeroJournals.objects.filter(
            organisation=tenant, transaction_source__transactions_id__in=touched))

    pending = list(XeroJournalsSource.objects.filter(
        organisation=tenant, processed=False).values_list('id', flat=True))
    if pending:
        periods |= _months_of(XeroJournals.objects.filter(journal_source_id__in=pending))

    # An exclusion rule changed since the last build re-shapes the months it
    # matches, even though no journal row moved.
    for ex in XeroJournalExclusion.objects.filter(organisation=tenant, updated_at__gt=last_build):
        if ex.date:
            periods.add((ex.date.year, ex.date.month))
        elif ex.journal_number is not None:
            periods |= _months_of(XeroJournals.objects.filter(
                organisation=tenant, journal_number=ex.journal_number))
        elif ex.journal_id:
            base = _re.sub(r'_[0-9]+$', '', ex.journal_id)
            periods |= _months_of(XeroJournals.objects.filter(organisation=tenant).filter(
                Q(journal_id=base) | Q(journal_id__startswith=base + '_')))
        else:
            # Description/reference-only rule: could match any month.
            print(f"[SCOPE] Exclusion {ex.pk} changed and has no date/journal key; full rebuild")
            return None

    print(f"[SCOPE] Incremental: {len(touched)} transactions, {len(pending)} pending "
          f"manual journals, {len(periods)} month(s) so far since {last_reprocess:%Y-%m-%d %H:%M}")
    return {
        'touched_transaction_ids': touched,
        'pending_journal_source_ids': pending,
        'affected_periods': periods,
    }


def process_xero_data(tenant_id, rebuild_trail_balance=False, exclude_manual_journals=False,
                      calculate_pnl_ytd=True, touched_transaction_ids=None,
                      affected_periods=None):
    """
    Service function to process Xero data (trail balance, etc.).
    Extracted from XeroProcessDataView for use in scheduled tasks.
    
    Processing order:
    1. Process journals from XeroJournalsSource to XeroJournals
    2. Create trail balance from processed journals
    3. Calculate balance_to_date for balance sheet accounts (ASSET, LIABILITY, EQUITY) (optional)
    
    Note: Metadata and Data Source updates must complete before this runs.
    
    Args:
        tenant_id: Xero tenant ID
        rebuild_trail_balance: If True, force full rebuild of trail balance and ignore existing data
        exclude_manual_journals: If True, only build trail balance from regular journals (exclude manual journals)
        calculate_pnl_ytd: If True (default), calculate balance_to_date for balance sheet accounts after trail balance. Set False to skip.
        touched_transaction_ids: Optional set of transaction IDs updated in the preceding sync step.
            When provided, only those transactions are reprocessed (incremental).
            When None, all transactions are reprocessed (full rebuild).
            Ignored when rebuild_trail_balance=True (always full rebuild).
        affected_periods: Optional list of (year, month) tuples updated in the preceding sync step.
    
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
    
    step_seconds = {}
    stats['step_seconds'] = step_seconds

    def _timed(name, started):
        step_seconds[name] = round(time.time() - started, 2)
        print(f"[PROCESS] {name}: {step_seconds[name]}s")

    try:
        # Step 0: decide the scope. Explicit arguments win (a caller that ran
        # the sync in-process knows exactly what it touched); otherwise derive
        # it from the database; rebuild=True forces everything.
        scope = None
        if not rebuild_trail_balance:
            if touched_transaction_ids is not None or affected_periods is not None:
                scope = {
                    'touched_transaction_ids': set(touched_transaction_ids or ()),
                    'pending_journal_source_ids': [],
                    'affected_periods': {tuple(int(x) for x in p) for p in (affected_periods or ())},
                }
            else:
                scope = derive_incremental_scope(tenant)
        stats['scope'] = 'full' if scope is None else 'incremental'

        # Step 1: Process journals from XeroJournalsSource to XeroJournals
        # When rebuilding trail balance, force reprocess to fix tracking assignment
        t0 = time.time()
        logger.info(f'Start Processing Journals for tenant {tenant_id}')
        print(f"[PROCESS] Starting journal processing for tenant {tenant_id}")
        process_journals(tenant_id, force_reprocess=rebuild_trail_balance)
        stats['journals_processed'] = True
        print(f"[PROCESS] ✓ Journals processed")
        if scope is not None and scope['pending_journal_source_ids']:
            # "After" months of the manual journals just (re)processed.
            scope['affected_periods'] |= _months_of(XeroJournals.objects.filter(
                journal_source_id__in=scope['pending_journal_source_ids']))
        _timed('process_journals', t0)

        # Step 1b: Reprocess transaction-based journals (invoices, bank transactions, etc.)
        # Full rebuild when explicitly requested; incremental when touched IDs are available.
        t0 = time.time()
        from apps.xero.xero_data.transaction_processor import process_transactions_to_journals
        txn_ids = None if scope is None else scope['touched_transaction_ids']
        mode = "FULL" if txn_ids is None else f"INCREMENTAL ({len(txn_ids)} transactions)"
        print(f"[PROCESS] Reprocessing transaction-based journals — {mode}")
        txn_stats = process_transactions_to_journals(tenant, touched_transaction_ids=txn_ids)
        print(f"[PROCESS] ✓ Transaction journals reprocessed: {txn_stats.get('journal_entries_created', 0)} created")
        stats['transaction_journals_reprocessed'] = True
        if txn_ids:
            # "After" months: a re-dated transaction lands in a new month.
            scope['affected_periods'] |= _months_of(XeroJournals.objects.filter(
                organisation=tenant, transaction_source__transactions_id__in=txn_ids))
        _timed('reprocess_transactions', t0)
        logger.info(f'Journals processed for tenant {tenant_id}')

        # Honest "last run" stamp for the console's Process Journals card.
        # This step has no Xero endpoint of its own, so without this the console
        # falls back to the legacy XeroLastUpdate['journals'] cursor (last written
        # 2025-11-25) and shows a months-old date as if it were the last run.
        # 'process_journals' is a display-only stamp — it is NOT a sync cursor
        # and nothing in the Xero client reads it.
        from apps.xero.xero_sync.models import XeroLastUpdate
        XeroLastUpdate.objects.update_or_create_timestamp('process_journals', tenant)

        # Step 2: Create trail balance from processed journals
        logger.info(f'Start Creating Trail Balance for tenant {tenant_id}')
        print(f"[PROCESS] Starting trail balance creation for tenant {tenant_id}")
        if rebuild_trail_balance:
            print(f"[PROCESS] REBUILD mode: forcing full rebuild of trail balance")
        if exclude_manual_journals:
            print(f"[PROCESS] Excluding manual journals - only using regular journals for trail balance")
        t0 = time.time()
        periods = None if scope is None else sorted(scope['affected_periods'])
        if periods is not None:
            print(f"[PROCESS] Trail balance scope: {len(periods)} month(s) {periods}")
            stats['affected_periods'] = [f'{y:04d}-{m:02d}' for y, m in periods]
        tb_result = create_trail_balance(
            tenant_id,
            incremental=not rebuild_trail_balance,
            rebuild=rebuild_trail_balance,
            exclude_manual_journals=exclude_manual_journals,
            affected_periods=periods,
        )
        stats['trail_balance_created'] = True
        trail_balance_skipped = bool(tb_result.get('skipped')) if isinstance(tb_result, dict) else False
        stats['trail_balance_skipped'] = trail_balance_skipped
        if trail_balance_skipped:
            print(f"[PROCESS] Trail balance unchanged; rebuild skipped")
        else:
            print(f"[PROCESS] ✓ Trail balance created")
        _timed('trail_balance', t0)

        # Step 3: Calculate balance_to_date for balance sheet accounts (optional)
        if calculate_pnl_ytd and not stats.get('trail_balance_skipped'):
            t0 = time.time()
            logger.info(f'Start calculating balance sheet balance_to_date for tenant {tenant_id}')
            print(f"[PROCESS] Starting balance sheet balance_to_date calculation for tenant {tenant_id}")
            calculate_balance_sheet_balance_to_date(tenant_id, affected_periods=periods)
            stats['pnl_balance_to_date_calculated'] = True
            print(f"[PROCESS] ✓ Balance sheet balance_to_date calculated")
            _timed('balance_to_date', t0)
        elif stats.get('trail_balance_skipped'):
            stats['pnl_balance_to_date_calculated'] = False
            print(f"[PROCESS] Skipped balance sheet balance_to_date calculation (no trail-balance changes)")
        else:
            stats['pnl_balance_to_date_calculated'] = False
            print(f"[PROCESS] Skipped balance sheet balance_to_date calculation (calculate_pnl_ytd=False)")
        
        # Uncomment if needed
        # create_balance_sheet(tenant_id)
        # stats['balance_sheet_created'] = True
        
        # Uncomment if needed
        # from apps.xero.xero_integration.services import export_accounts
        # export_accounts(tenant_id)
        # stats['accounts_exported'] = True
        
        duration = time.time() - start_time
        stats['duration_seconds'] = round(duration, 2)
        # The one line to grep for when the build gets slow again.
        logger.info('process_xero_data tenant=%s scope=%s duration=%.1fs steps=%s',
                    tenant_id, stats['scope'], duration, step_seconds)
        print(f"[PROCESS] Done in {duration:.1f}s ({stats['scope']}): {step_seconds}")

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


def import_pnl_by_tracking(tenant_id, from_date=None, to_date=None, periods=11, user=None,
                           include_tracking=True):
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
        include_tracking: When False, only import the unfiltered/overall Xero
            P&L. Use this for faster reconciliation checks where tracking-level
            detail is not needed.

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

    # Default date range: last 12 months. Longer explicit ranges are supported
    # below by splitting them into month-anchored API calls, keeping each Xero
    # request inside the API's 365-day report limit.
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

    if from_date > to_date:
        raise ValueError(f"from_date {from_date} must be before or equal to to_date {to_date}.")

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
        'include_tracking': bool(include_tracking),
    }

    # Delete only the date range being imported so historical backfills can run
    # safely without wiping previously imported periods. Overall-only imports
    # must not delete tracking detail that was fetched by a fuller import.
    delete_qs = XeroPnlByTracking.objects.filter(
        organisation=organisation,
        year__gte=from_date.year,
        year__lte=to_date.year,
    ).filter(
        models.Q(year__gt=from_date.year) |
        models.Q(year=from_date.year, month__gte=from_date.month)
    ).filter(
        models.Q(year__lt=to_date.year) |
        models.Q(year=to_date.year, month__lte=to_date.month)
    )
    if not include_tracking:
        delete_qs = delete_qs.filter(tracking__isnull=True)
    delete_qs.delete()

    # ------------------------------------------------------------------
    # 3a. Pull P&L for each tracking option
    # ------------------------------------------------------------------
    if include_tracking:
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
    else:
        print("[PNL-TRACKING] Skipping tracking-option P&L; importing OVERALL only")

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
