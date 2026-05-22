"""
Orchestrate the full Planning Analytics pipeline:
  1. Update Metadata (accounts, contacts, tracking categories)
  2. Update Postgres (sync from Xero)
  3. Process Journals + Build Trail Balance (optionally with P&L YTD)
  4. Execute TM1 TI processes
"""
import time
from django.db import connection
from apps.planning_analytics.services.tm1_client import execute_process, _resolve_credentials


def _load_tm1_processes_from_db():
    """Return list of dicts [{name, parameters}, ...] from DB config."""
    from apps.planning_analytics.models import TM1ProcessConfig
    return [
        {'name': p.process_name, 'parameters': p.parameters or {}}
        for p in TM1ProcessConfig.objects.filter(enabled=True)
    ]


def _pipeline_lock_key(tenant_id):
    return f"planning_pipeline:{tenant_id}"


def _try_acquire_pipeline_lock(tenant_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [_pipeline_lock_key(tenant_id)])
        return bool(cursor.fetchone()[0])


def _release_pipeline_lock(tenant_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", [_pipeline_lock_key(tenant_id)])


def run_pipeline(
    tenant_id,
    load_all=False,
    rebuild_trail_balance=False,
    exclude_manual_journals=False,
    calculate_pnl_ytd=True,
    tm1_processes=None,
    tm1_user=None,
    tm1_password=None,
):
    """
    Run the full pipeline and return step-by-step results.

    Args:
        tenant_id: Xero tenant ID
        load_all: pass to update_financial_data
        rebuild_trail_balance: pass to process_xero_data
        exclude_manual_journals: pass to process_xero_data
        calculate_pnl_ytd: pass to process_xero_data
        tm1_processes: list of {name, parameters} dicts. If None, loaded from DB.
    """
    results = []
    touched_transaction_ids = None
    affected_periods = None
    lock_acquired = _try_acquire_pipeline_lock(tenant_id)
    if not lock_acquired:
        return {
            'steps': [{
                'step': 'pipeline_lock',
                'success': False,
                'message': 'A pipeline run is already active for this tenant.',
                'elapsed_s': 0,
            }]
        }

    try:
        # Step 1 - Update Metadata (accounts, contacts, tracking categories)
        step = {'step': 'update_metadata', 'success': False}
        t0 = time.time()
        try:
            from apps.xero.xero_metadata.services import update_metadata
            meta_result = update_metadata(tenant_id)
            step['success'] = meta_result.get('status') != 'error'
            step['message'] = meta_result.get('message', 'Metadata updated')
            step['stats'] = meta_result.get('stats')
        except Exception as exc:
            step['message'] = str(exc)
        step['elapsed_s'] = round(time.time() - t0, 1)
        results.append(step)

        # Step 2 - Update Postgres (capture touched_transaction_ids for incremental processing)
        step = {'step': 'update_postgres', 'success': False}
        t0 = time.time()
        try:
            from apps.xero.xero_data.services import update_financial_data
            sync_result = update_financial_data(tenant_id, load_all=load_all)
            if 'touched_transaction_ids' in sync_result:
                touched_transaction_ids = sync_result.get('touched_transaction_ids')
            else:
                touched_transaction_ids = None
            affected_periods = sync_result.get('affected_periods')
            step['success'] = True
            step['message'] = 'Postgres updated from Xero'
            step['stats'] = sync_result.get('stats')
        except Exception as exc:
            step['message'] = str(exc)
        step['elapsed_s'] = round(time.time() - t0, 1)
        results.append(step)

        # Step 3 - Process Journals + Trail Balance + optional YTD
        step = {'step': 'process_xero_data', 'success': False}
        t0 = time.time()
        try:
            from apps.xero.xero_cube.services import process_xero_data
            result = process_xero_data(
                tenant_id,
                rebuild_trail_balance=rebuild_trail_balance,
                exclude_manual_journals=exclude_manual_journals,
                calculate_pnl_ytd=calculate_pnl_ytd,
                touched_transaction_ids=touched_transaction_ids,
                affected_periods=affected_periods,
            )
            step['success'] = True
            step['message'] = result.get('message', 'Xero data processed')
            step['stats'] = result.get('stats')
        except Exception as exc:
            step['message'] = str(exc)
        step['elapsed_s'] = round(time.time() - t0, 1)
        results.append(step)

        # Step 4 - TM1 processes
        tm1_should_skip = (
            not rebuild_trail_balance
            and affected_periods is not None
            and len(affected_periods) == 0
        )
        if tm1_should_skip:
            results.append({
                'step': 'tm1',
                'success': True,
                'message': 'Skipped TM1 refresh because Xero sync had no affected periods.',
                'elapsed_s': 0,
            })
            return {'steps': results}

        if tm1_processes is None:
            tm1_processes = _load_tm1_processes_from_db()

        for proc in tm1_processes:
            name = proc.get('name') or proc.get('process_name', 'unknown')
            params = proc.get('parameters', {})
            step = {'step': f'tm1:{name}', 'success': False}
            t0 = time.time()
            res = execute_process(name, parameters=params if params else None, user=tm1_user, password=tm1_password)
            step['success'] = res.get('success', False)
            step['message'] = res.get('message', '')
            step['elapsed_s'] = round(time.time() - t0, 1)
            results.append(step)

        return {'steps': results}
    finally:
        _release_pipeline_lock(tenant_id)
