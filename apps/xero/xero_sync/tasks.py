"""
Scheduled tasks for Xero data synchronization.
Uses APScheduler to run tasks hourly per tenant.
"""
import logging
import time
from django.utils import timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from apps.xero.xero_core.exceptions import DailyLimitReached
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_sync.models import XeroTenantSchedule, XeroTaskExecutionLog
from apps.xero.xero_sync.services import update_xero_models
from apps.xero.xero_cube.services import process_xero_data, process_profit_loss

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler = None


def run_update_task(tenant_id):
    """
    Run update models task for a specific tenant.
    This is called by the scheduler.
    """
    log_entry = None
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        schedule = XeroTenantSchedule.objects.get(tenant=tenant)
        
        # Check if task should run
        if not schedule.should_run_update():
            logger.info(f"Skipping update task for tenant {tenant_id} - not scheduled yet")
            return
        
        # Create log entry
        log_entry = XeroTaskExecutionLog.objects.create(
            tenant=tenant,
            task_type='update_models',
            status='running'
        )
        
        logger.info(f"Starting update task for tenant {tenant_id}")
        start_time = time.time()
        
        # Run the update
        result = update_xero_models(tenant_id)
        
        duration = time.time() - start_time
        
        # Update schedule
        schedule.last_update_run = timezone.now()
        schedule.update_next_run_times()
        
        # Mark log as completed with API call count
        records_processed = sum([
            result['stats'].get('accounts_updated', 0),
            result['stats'].get('contacts_updated', 0),
            result['stats'].get('tracking_categories_updated', 0),
            result['stats'].get('bank_transactions_updated', 0),
            result['stats'].get('invoices_updated', 0),
            result['stats'].get('payments_updated', 0),
            result['stats'].get('journals_updated', 0),
        ])
        
        # Log API calls
        api_calls = result['stats'].get('api_calls', 0)
        logger.info(f"Update task for tenant {tenant_id} made {api_calls} API calls")
        
        log_entry.mark_completed(
            duration_seconds=duration,
            records_processed=records_processed,
            stats=result.get('stats', {})
        )
        
        logger.info(f"Completed update task for tenant {tenant_id} in {duration:.2f} seconds")
        
        # Check if metadata and data source updates completed successfully
        # These must complete before process task runs
        from apps.xero.xero_sync.models import XeroLastUpdate
        metadata_endpoints = ['accounts', 'contacts', 'tracking_categories']
        data_endpoints = ['journals', 'manual_journals']
        
        # Check metadata completion
        metadata_complete = True
        for endpoint in metadata_endpoints:
            try:
                last_update = XeroLastUpdate.objects.get(
                    end_point=endpoint,
                    organisation=organisation
                )
                if not last_update.end_time:
                    metadata_complete = False
                    break
            except XeroLastUpdate.DoesNotExist:
                metadata_complete = False
                break
        
        # Check data source completion
        data_complete = True
        for endpoint in data_endpoints:
            try:
                last_update = XeroLastUpdate.objects.get(
                    end_point=endpoint,
                    organisation=organisation
                )
                if not last_update.end_time:
                    data_complete = False
                    break
            except XeroLastUpdate.DoesNotExist:
                data_complete = False
                break
        
        # Only trigger process task if update was successful AND metadata/data completed
        if result.get('success', False) and metadata_complete and data_complete:
            logger.info(f"Triggering process task for tenant {tenant_id} after update completion")
            try:
                run_process_task(tenant_id)
            except Exception as e:
                logger.error(f"Error running process task after update for {tenant_id}: {str(e)}", exc_info=True)
        else:
            if not metadata_complete:
                logger.warning(f"Skipping process task for tenant {tenant_id} - metadata updates not complete")
            elif not data_complete:
                logger.warning(f"Skipping process task for tenant {tenant_id} - data source updates not complete")
            else:
                logger.warning(f"Skipping process task for tenant {tenant_id} due to update errors")
        
    except XeroTenant.DoesNotExist:
        error_msg = f"Tenant {tenant_id} not found"
        logger.error(error_msg)
        if log_entry:
            log_entry.mark_failed(error_msg)
    except XeroTenantSchedule.DoesNotExist:
        error_msg = f"Schedule not configured for tenant {tenant_id}"
        logger.warning(error_msg)
        if log_entry:
            log_entry.status = 'skipped'
            log_entry.error_message = error_msg
            log_entry.completed_at = timezone.now()
            log_entry.save()
    except DailyLimitReached as e:
        # Clean abort: the tenant's daily Xero budget is gone, so retrying now is
        # pointless. Mark the run failed and return so the scheduler slot is
        # released (a hung/looping job here is what jammed max_instances=1 in the
        # 2026-08-18 incident); a later run after the reset will catch up.
        error_msg = f"Update task aborted for tenant {tenant_id}: {str(e)}"
        logger.warning(error_msg)
        if log_entry:
            log_entry.mark_failed(error_msg)
        logger.warning(f"Skipping process task for tenant {tenant_id} - Xero daily API limit reached")
    except ValueError as e:
        # Handle authentication/token errors specifically
        error_msg = f"Update task failed for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        if log_entry:
            log_entry.mark_failed(error_msg)
        # Don't trigger process task if update failed due to authentication
        logger.warning(f"Skipping process task for tenant {tenant_id} due to update failure")
    except Exception as e:
        error_msg = f"Update task failed for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        if log_entry:
            log_entry.mark_failed(error_msg)
        # Don't trigger process task if update failed
        logger.warning(f"Skipping process task for tenant {tenant_id} due to update failure")


def run_process_task(tenant_id):
    """
    Run process data task for a specific tenant.
    This runs after update task completes.
    """
    log_entry = None
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        schedule = XeroTenantSchedule.objects.get(tenant=tenant)
        
        # Check if task should run (only after update has run)
        if not schedule.should_run_process():
            logger.info(f"Skipping process task for tenant {tenant_id} - update not completed yet")
            return
        
        # Create log entry
        log_entry = XeroTaskExecutionLog.objects.create(
            tenant=tenant,
            task_type='process_data',
            status='running'
        )
        
        logger.info(f"Starting process task for tenant {tenant_id}")
        start_time = time.time()
        
        # Run the process
        result = process_xero_data(tenant_id)
        
        duration = time.time() - start_time
        
        # Update schedule - process doesn't have separate schedule, just track when it ran
        schedule.last_process_run = timezone.now()
        schedule.save()  # Don't call update_next_run_times for process - it's tied to update
        
        # Mark log as completed
        records_processed = 0  # Trail balance doesn't return record count easily
        log_entry.mark_completed(
            duration_seconds=duration,
            records_processed=records_processed,
            stats=result.get('stats', {})
        )
        
        logger.info(f"Completed process task for tenant {tenant_id} in {duration:.2f} seconds")
        
        # Immediately trigger P&L task after process completes (only if process was successful)
        if result.get('success', False):
            logger.info(f"Triggering P&L task for tenant {tenant_id} after process completion")
            try:
                run_profit_loss_task(tenant_id)
            except Exception as e:
                logger.error(f"Error running P&L task after process for {tenant_id}: {str(e)}", exc_info=True)
        else:
            logger.warning(f"Skipping P&L task for tenant {tenant_id} due to process errors")
        
    except XeroTenant.DoesNotExist:
        error_msg = f"Tenant {tenant_id} not found"
        logger.error(error_msg)
        if log_entry:
            log_entry.mark_failed(error_msg)
    except XeroTenantSchedule.DoesNotExist:
        error_msg = f"Schedule not configured for tenant {tenant_id}"
        logger.warning(error_msg)
        if log_entry:
            log_entry.status = 'skipped'
            log_entry.error_message = error_msg
            log_entry.completed_at = timezone.now()
            log_entry.save()
    except Exception as e:
        error_msg = f"Process task failed for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        if log_entry:
            log_entry.mark_failed(error_msg)


def run_profit_loss_task(tenant_id):
    """
    Run Profit & Loss processing task for a specific tenant.
    This runs after process task completes.
    """
    log_entry = None
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        schedule = XeroTenantSchedule.objects.get(tenant=tenant)
        
        # Create log entry
        log_entry = XeroTaskExecutionLog.objects.create(
            tenant=tenant,
            task_type='process_data',  # Using same type for now
            status='running'
        )
        
        logger.info(f"Starting P&L task for tenant {tenant_id}")
        start_time = time.time()
        
        # Run the P&L processing
        result = process_profit_loss(tenant_id)
        
        duration = time.time() - start_time
        
        # Mark log as completed
        log_entry.mark_completed(
            duration_seconds=duration,
            records_processed=0,
            stats=result.get('stats', {})
        )
        
        logger.info(f"Completed P&L task for tenant {tenant_id} in {duration:.2f} seconds")
        
    except XeroTenant.DoesNotExist:
        error_msg = f"Tenant {tenant_id} not found"
        logger.error(error_msg)
        if log_entry:
            log_entry.mark_failed(error_msg)
    except XeroTenantSchedule.DoesNotExist:
        error_msg = f"Schedule not configured for tenant {tenant_id}"
        logger.warning(error_msg)
        if log_entry:
            log_entry.status = 'skipped'
            log_entry.error_message = error_msg
            log_entry.completed_at = timezone.now()
            log_entry.save()
    except Exception as e:
        error_msg = f"P&L task failed for tenant {tenant_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        if log_entry:
            log_entry.mark_failed(error_msg)


def run_process_tree_task(tree_name: str):
    """
    Run a scheduled process tree task.
    This is called by the scheduler.
    """
    log_entry = None
    try:
        from apps.xero.xero_sync.models import ProcessTree, ProcessTreeSchedule
        from apps.xero.xero_sync.process_manager.tree_builder import ProcessTreeManager
        
        process_tree = ProcessTree.objects.get(name=tree_name)
        schedule = ProcessTreeSchedule.objects.get(process_tree=process_tree)
        
        # Check if task should run
        if not schedule.should_run():
            logger.info(f"Skipping process tree task '{tree_name}' - not scheduled yet")
            return
        
        logger.info(f"Starting process tree task: {tree_name}")
        start_time = time.time()
        
        # Execute the process tree
        results = ProcessTreeManager.execute_tree(
            tree_name,
            context=schedule.context or {},
            func_registry={}  # Functions should be registered in ProcessTreeInstance
        )
        
        duration = time.time() - start_time
        
        # Update schedule
        schedule.last_run = timezone.now()
        schedule.update_next_run_time()
        
        if results.get('success'):
            logger.info(f"Completed process tree task '{tree_name}' in {duration:.2f} seconds")
        else:
            logger.error(f"Process tree task '{tree_name}' failed: {results.get('errors', [])}")
            
    except ProcessTree.DoesNotExist:
        error_msg = f"Process tree '{tree_name}' not found"
        logger.error(error_msg)
    except ProcessTreeSchedule.DoesNotExist:
        error_msg = f"Schedule not configured for process tree '{tree_name}'"
        logger.warning(error_msg)
    except Exception as e:
        error_msg = f"Process tree task failed for '{tree_name}': {str(e)}"
        logger.error(error_msg, exc_info=True)


def check_and_run_scheduled_tasks():
    """
    Check all tenant schedules and run tasks that are due.
    This function runs every minute to check for due tasks.
    Also checks for scheduled process trees.
    """
    try:
        # Check tenant schedules
        schedules = XeroTenantSchedule.objects.filter(enabled=True)
        
        for schedule in schedules:
            tenant_id = schedule.tenant.tenant_id
            
            # Check and run update task if due
            # Process task will be triggered automatically after update completes
            if schedule.should_run_update():
                try:
                    run_update_task(tenant_id)
                    # Note: run_update_task now automatically triggers run_process_task
                except Exception as e:
                    logger.error(f"Error running update task for {tenant_id}: {str(e)}", exc_info=True)
        
        # Check process tree schedules
        from apps.xero.xero_sync.models import ProcessTreeSchedule
        process_tree_schedules = ProcessTreeSchedule.objects.filter(enabled=True)
        
        for schedule in process_tree_schedules:
            if schedule.should_run():
                try:
                    run_process_tree_task(schedule.process_tree.name)
                except Exception as e:
                    logger.error(f"Error running process tree task '{schedule.process_tree.name}': {str(e)}", exc_info=True)
                    
    except Exception as e:
        logger.error(f"Error in scheduled task checker: {str(e)}", exc_info=True)


def check_out_of_sync_background():
    """
    Background task to check and retry out-of-sync items.
    This should run at predetermined intervals (e.g., every hour).
    """
    try:
        from apps.xero.xero_sync.services_sync_check import run_background_sync_check
        run_background_sync_check()
    except Exception as e:
        logger.error(f"Error in background sync check: {str(e)}", exc_info=True)


def start_scheduler():
    """Start the APScheduler background scheduler."""
    global scheduler
    
    if scheduler and scheduler.running:
        logger.warning("Scheduler is already running")
        return
    
    scheduler = BackgroundScheduler()
    
    # Schedule the checker to run every minute
    scheduler.add_job(
        check_and_run_scheduled_tasks,
        trigger=IntervalTrigger(minutes=1),
        id='xero_scheduled_tasks_checker',
        name='Check and run Xero scheduled tasks',
        replace_existing=True,
    )
    
    # Schedule background sync check to run every hour
    scheduler.add_job(
        check_out_of_sync_background,
        trigger=IntervalTrigger(hours=1),
        id='xero_background_sync_check',
        name='Check and retry out-of-sync items',
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info("Xero task scheduler started - checking for due tasks every minute, sync check every hour")


def stop_scheduler():
    """Stop the APScheduler background scheduler."""
    global scheduler
    
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("Xero task scheduler stopped")
    else:
        logger.warning("Scheduler is not running")

