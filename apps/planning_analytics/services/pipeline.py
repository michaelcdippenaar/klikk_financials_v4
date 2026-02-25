"""
Orchestrate the full Planning Analytics pipeline:
  1. Update Metadata (accounts, contacts, tracking categories)
  2. Update Postgres (sync from Xero)
  3. Process Journals + Build Trail Balance (optionally with P&L YTD)
  4. Execute TM1 TI processes
"""
import time
from apps.planning_analytics.services.tm1_client import execute_process, _resolve_credentials


def _load_tm1_processes_from_db():
    """Return list of dicts [{name, parameters}, ...] from DB config."""
    from apps.planning_analytics.models import TM1ProcessConfig
    return [
        {'name': p.process_name, 'parameters': p.parameters or {}}
        for p in TM1ProcessConfig.objects.filter(enabled=True)
    ]


def run_pipeline(
    tenant_id,
    load_all=False,
    rebuild_trail_balance=False,
    exclude_manual_journals=False,
    calculate_pnl_ytd=True,
    tm1_processes=None,
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

    # Step 2 - Update Postgres
    step = {'step': 'update_postgres', 'success': False}
    t0 = time.time()
    try:
        from apps.xero.xero_data.services import update_financial_data
        update_financial_data(tenant_id, load_all=load_all)
        step['success'] = True
        step['message'] = 'Postgres updated from Xero'
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
        )
        step['success'] = True
        step['message'] = result.get('message', 'Xero data processed')
        step['stats'] = result.get('stats')
    except Exception as exc:
        step['message'] = str(exc)
    step['elapsed_s'] = round(time.time() - t0, 1)
    results.append(step)

    # Step 4 - TM1 processes
    if tm1_processes is None:
        tm1_processes = _load_tm1_processes_from_db()

    for proc in tm1_processes:
        name = proc.get('name') or proc.get('process_name', 'unknown')
        params = proc.get('parameters', {})
        step = {'step': f'tm1:{name}', 'success': False}
        t0 = time.time()
        res = execute_process(name, parameters=params if params else None)
        step['success'] = res.get('success', False)
        step['message'] = res.get('message', '')
        step['elapsed_s'] = round(time.time() - t0, 1)
        results.append(step)

    return {'steps': results}
