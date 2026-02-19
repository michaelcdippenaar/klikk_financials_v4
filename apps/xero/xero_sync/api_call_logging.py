"""
Xero API call logging for rate limit tracking.
"""
from django.utils import timezone
from django.db.models import Sum


def log_xero_api_calls(process, api_calls, tenant=None):
    """
    Log Xero API call count for a process run.

    Args:
        process: Process identifier (metadata, data, journals, trail-balance, pnl-by-tracking, reconcile)
        api_calls: Number of API calls made
        tenant: XeroTenant instance or None
    """
    from apps.xero.xero_sync.models import XeroApiCallLog

    XeroApiCallLog.objects.create(
        process=process,
        tenant=tenant,
        api_calls=api_calls or 0,
    )


def get_api_call_stats(tenant_id=None):
    """
    Get API call statistics for display in Admin Console.

    Returns per-process stats: last run count, today's count.
    Returns total today across all processes.

    Args:
        tenant_id: Optional tenant ID to filter by tenant

    Returns:
        dict: {
            "by_process": {
                "metadata": {"last_run": 4, "today": 12},
                "data": {"last_run": 50, "today": 150},
                ...
            },
            "total_today": 162,
        }
    """
    from apps.xero.xero_sync.models import XeroApiCallLog

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    qs = XeroApiCallLog.objects.all()
    if tenant_id:
        qs = qs.filter(tenant_id=tenant_id)

    # Per process: last run + today total
    by_process = {}
    for process_id, _ in XeroApiCallLog.PROCESS_CHOICES:
        process_logs = qs.filter(process=process_id).order_by('-created_at')

        last_run = 0
        last_entry = process_logs.first()
        if last_entry:
            last_run = last_entry.api_calls

        today_total = (
            process_logs.filter(created_at__gte=today_start).aggregate(
                total=Sum('api_calls')
            )['total']
            or 0
        )

        by_process[process_id] = {
            'last_run': last_run,
            'today': today_total,
        }

    total_today = sum(p['today'] for p in by_process.values())

    return {
        'by_process': by_process,
        'total_today': total_today,
    }
