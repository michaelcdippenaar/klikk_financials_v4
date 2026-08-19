"""
Xero API call logging for rate limit tracking.
"""
from django.conf import settings
from django.utils import timezone
from django.db.models import Sum


def log_xero_api_calls(process, api_calls, tenant=None):
    """
    Log Xero API call count for a process run.

    Args:
        process: Process identifier -- one of XeroApiCallLog.PROCESS_CHOICES
            (metadata, data, journals, trail-balance, pnl-by-tracking, reconcile,
            invoices, quotes, documents). A value outside that list is stored
            but never counted by get_api_call_stats().
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

    Two signals, in order of trust:

    * quota -- Xero's OWN allowance from the X-DayLimit-Remaining /
      X-MinLimit-Remaining headers, persisted by XeroApiClient into
      XeroApiQuota. This is the number to display when present. It is
      best-effort / throttled (~15s lag) and absent until a tenant has made
      at least one call since the table was added.
    * by_process / total_today -- the locally logged count from
      XeroApiCallLog. Only process runners log, so probes, backfills and
      one-shot scripts are invisible here and the total UNDER-reports. Use it
      only as the fallback when quota.day_remaining is None.

    Args:
        tenant_id: Optional tenant ID to filter by tenant. Without it, the
            quota block comes from whichever tenant was seen most recently.

    Returns:
        dict: {
            "cap": 1000,
            "by_process": {
                "metadata": {"last_run": 4, "today": 12},
                "data": {"last_run": 50, "today": 150},
                ...
            },
            "total_today": 162,
            "quota": {
                "cap": 1000,
                "day_remaining": 946,        # None when no XeroApiQuota row
                "min_remaining": 58,
                "used_estimate": 54,         # cap - day_remaining, >= 0
                "seen_at": "2026-08-19T12:34:56+00:00",
                "last_status": 200,
                "tenant_id": "...",
            },
        }
    """
    from apps.xero.xero_core.models import XeroApiQuota
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

    cap = settings.XERO_DAILY_CALL_CAP
    quota = {
        'cap': cap,
        'day_remaining': None,
        'min_remaining': None,
        'used_estimate': None,
        'seen_at': None,
        'last_status': None,
        'tenant_id': None,
    }
    quota_qs = XeroApiQuota.objects.all()
    if tenant_id:
        quota_qs = quota_qs.filter(tenant_id=tenant_id)
    quota_row = quota_qs.order_by('-seen_at').first()
    if quota_row is not None:
        day_remaining = quota_row.day_remaining
        quota.update({
            'day_remaining': day_remaining,
            'min_remaining': quota_row.min_remaining,
            'used_estimate': (
                max(cap - day_remaining, 0) if day_remaining is not None else None
            ),
            'seen_at': quota_row.seen_at.isoformat() if quota_row.seen_at else None,
            'last_status': quota_row.last_status,
            'tenant_id': quota_row.tenant_id,
        })

    return {
        'cap': cap,
        'by_process': by_process,
        'total_today': total_today,
        'quota': quota,
    }
