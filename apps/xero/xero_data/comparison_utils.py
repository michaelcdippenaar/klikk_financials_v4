"""
Comparison utilities for journal totals (e.g. for reconciliation against another source).

Usage:
    from apps.xero.xero_data.comparison_utils import (
        compare_totals, compare_by_period, generate_report, get_journals_totals
    )
    result = compare_totals(tenant_id='your-tenant-id')
    report = generate_report(tenant_id='your-tenant-id')
"""
import logging
from decimal import Decimal
from django.db.models import Sum, Count, F
from django.db.models.functions import Extract

logger = logging.getLogger(__name__)


def get_journals_totals(organisation, journal_types=None):
    """
    Get total amounts by account from journals.
    
    Args:
        organisation: XeroTenant instance
        journal_types: List of journal types to include (None = all)
    
    Returns:
        dict: {account_code: {total: Decimal, count: int}}
    """
    from apps.xero.xero_data.models import XeroJournals
    
    qs = XeroJournals.objects.filter(organisation=organisation)
    
    if journal_types:
        qs = qs.filter(journal_type__in=journal_types)
    
    totals = qs.values(
        'account__code',
        'account__name',
        'account__type'
    ).annotate(
        total=Sum('amount'),
        count=Count('id')
    ).order_by('account__code')
    
    return {
        t['account__code']: {
            'name': t['account__name'],
            'type': t['account__type'],
            'total': t['total'] or Decimal('0'),
            'count': t['count']
        }
        for t in totals
    }


def get_period_totals(organisation, journal_types=None):
    """
    Get total amounts by account and period from journals.
    
    Args:
        organisation: XeroTenant instance
        journal_types: List of journal types to include (None = all)
    
    Returns:
        dict: {(account_code, year, month): {total: Decimal, count: int}}
    """
    from apps.xero.xero_data.models import XeroJournals
    
    qs = XeroJournals.objects.filter(organisation=organisation)
    
    if journal_types:
        qs = qs.filter(journal_type__in=journal_types)
    
    totals = qs.annotate(
        year=Extract('date', 'year'),
        month=Extract('date', 'month')
    ).values(
        'account__code',
        'year',
        'month'
    ).annotate(
        total=Sum('amount'),
        count=Count('id')
    ).order_by('account__code', 'year', 'month')
    
    return {
        (t['account__code'], t['year'], t['month']): {
            'total': t['total'] or Decimal('0'),
            'count': t['count']
        }
        for t in totals
    }


def compare_totals(tenant_id):
    """
    Compare account totals between old and new pipelines.
    
    Args:
        tenant_id: Xero tenant ID
    
    Returns:
        dict: Comparison results
    """
    from apps.xero.xero_core.models import XeroTenant
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return {'error': f'Tenant {tenant_id} not found'}
    
    # Manual journal totals (and any legacy 'journal' type if present)
    old_totals = get_journals_totals(organisation, ['journal', 'manual_journal'])
    
    # Transaction-sourced journal totals
    new_totals = get_journals_totals(organisation, ['transaction'])
    
    # All accounts
    all_accounts = set(old_totals.keys()) | set(new_totals.keys())
    
    results = {
        'matching': [],
        'discrepancies': [],
        'old_only': [],
        'new_only': []
    }
    
    for account_code in sorted(all_accounts):
        old = old_totals.get(account_code, {'total': Decimal('0'), 'count': 0})
        new = new_totals.get(account_code, {'total': Decimal('0'), 'count': 0})
        
        if account_code in old_totals and account_code not in new_totals:
            results['old_only'].append({
                'account_code': account_code,
                'name': old.get('name', ''),
                'total': old['total'],
                'count': old['count']
            })
        elif account_code not in old_totals and account_code in new_totals:
            results['new_only'].append({
                'account_code': account_code,
                'name': new.get('name', ''),
                'total': new['total'],
                'count': new['count']
            })
        elif old['total'] == new['total']:
            results['matching'].append({
                'account_code': account_code,
                'name': old.get('name', ''),
                'total': old['total'],
                'old_count': old['count'],
                'new_count': new['count']
            })
        else:
            results['discrepancies'].append({
                'account_code': account_code,
                'name': old.get('name', ''),
                'old_total': old['total'],
                'new_total': new['total'],
                'difference': old['total'] - new['total'],
                'old_count': old['count'],
                'new_count': new['count']
            })
    
    return {
        'tenant_id': tenant_id,
        'tenant_name': organisation.tenant_name,
        'summary': {
            'matching_count': len(results['matching']),
            'discrepancy_count': len(results['discrepancies']),
            'old_only_count': len(results['old_only']),
            'new_only_count': len(results['new_only']),
        },
        **results
    }


def compare_by_period(tenant_id, year=None, month=None):
    """
    Compare totals by period between pipelines.
    
    Args:
        tenant_id: Xero tenant ID
        year: Optional year filter
        month: Optional month filter
    
    Returns:
        dict: Period-level comparison
    """
    from apps.xero.xero_core.models import XeroTenant
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return {'error': f'Tenant {tenant_id} not found'}
    
    # Get period totals
    old_periods = get_period_totals(organisation, ['journal', 'manual_journal'])
    new_periods = get_period_totals(organisation, ['transaction'])
    
    # Apply filters
    if year:
        old_periods = {k: v for k, v in old_periods.items() if k[1] == year}
        new_periods = {k: v for k, v in new_periods.items() if k[1] == year}
    if month:
        old_periods = {k: v for k, v in old_periods.items() if k[2] == month}
        new_periods = {k: v for k, v in new_periods.items() if k[2] == month}
    
    # All keys
    all_keys = set(old_periods.keys()) | set(new_periods.keys())
    
    discrepancies = []
    
    for key in sorted(all_keys):
        old = old_periods.get(key, {'total': Decimal('0')})
        new = new_periods.get(key, {'total': Decimal('0')})
        
        if old['total'] != new['total']:
            discrepancies.append({
                'account_code': key[0],
                'year': key[1],
                'month': key[2],
                'old_total': old['total'],
                'new_total': new['total'],
                'difference': old['total'] - new['total']
            })
    
    return {
        'tenant_id': tenant_id,
        'tenant_name': organisation.tenant_name,
        'filter': {'year': year, 'month': month},
        'discrepancy_count': len(discrepancies),
        'discrepancies': discrepancies
    }


def get_tracking_totals(organisation, journal_types=None):
    """
    Get totals by tracking category.
    
    Args:
        organisation: XeroTenant instance
        journal_types: List of journal types to include
    
    Returns:
        dict: Tracking category totals
    """
    from apps.xero.xero_data.models import XeroJournals
    
    qs = XeroJournals.objects.filter(organisation=organisation)
    
    if journal_types:
        qs = qs.filter(journal_type__in=journal_types)
    
    totals = qs.values(
        'account__code',
        'tracking1__option',
        'tracking2__option'
    ).annotate(
        total=Sum('amount')
    ).order_by('account__code')
    
    return list(totals)


def compare_tracking(tenant_id):
    """
    Compare tracking category breakdowns between pipelines.
    
    Args:
        tenant_id: Xero tenant ID
    
    Returns:
        dict: Tracking comparison
    """
    from apps.xero.xero_core.models import XeroTenant
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return {'error': f'Tenant {tenant_id} not found'}
    
    old_tracking = get_tracking_totals(organisation, ['journal', 'manual_journal'])
    new_tracking = get_tracking_totals(organisation, ['transaction'])
    
    # Convert to comparable format
    def to_key(row):
        return (
            row['account__code'],
            row['tracking1__option'] or '',
            row['tracking2__option'] or ''
        )
    
    old_dict = {to_key(r): r['total'] for r in old_tracking}
    new_dict = {to_key(r): r['total'] for r in new_tracking}
    
    all_keys = set(old_dict.keys()) | set(new_dict.keys())
    
    discrepancies = []
    
    for key in sorted(all_keys):
        old_total = old_dict.get(key, Decimal('0'))
        new_total = new_dict.get(key, Decimal('0'))
        
        if old_total != new_total:
            discrepancies.append({
                'account_code': key[0],
                'tracking1': key[1],
                'tracking2': key[2],
                'old_total': old_total,
                'new_total': new_total,
                'difference': old_total - new_total
            })
    
    return {
        'tenant_id': tenant_id,
        'discrepancy_count': len(discrepancies),
        'discrepancies': discrepancies
    }


def generate_report(tenant_id, output_format='dict'):
    """
    Generate a comprehensive comparison report.
    
    Args:
        tenant_id: Xero tenant ID
        output_format: 'dict' or 'text'
    
    Returns:
        Comparison report in requested format
    """
    from apps.xero.xero_core.models import XeroTenant
    from apps.xero.xero_data.models import XeroJournals
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return {'error': f'Tenant {tenant_id} not found'}
    
    # Get counts
    old_count = XeroJournals.objects.filter(
        organisation=organisation,
        journal_type__in=['journal', 'manual_journal']
    ).count()
    
    new_count = XeroJournals.objects.filter(
        organisation=organisation,
        journal_type='transaction'
    ).count()
    
    # Run comparisons
    total_comparison = compare_totals(tenant_id)
    period_comparison = compare_by_period(tenant_id)
    tracking_comparison = compare_tracking(tenant_id)
    
    report = {
        'tenant_id': tenant_id,
        'tenant_name': organisation.tenant_name,
        'journal_counts': {
            'old_pipeline': old_count,
            'new_pipeline': new_count
        },
        'total_comparison': total_comparison,
        'period_comparison': period_comparison,
        'tracking_comparison': tracking_comparison,
        'is_matching': (
            total_comparison['summary']['discrepancy_count'] == 0 and
            period_comparison['discrepancy_count'] == 0 and
            tracking_comparison['discrepancy_count'] == 0
        )
    }
    
    if output_format == 'text':
        return format_report_text(report)
    
    return report


def format_report_text(report):
    """
    Format a report as readable text.
    
    Args:
        report: Report dictionary
    
    Returns:
        str: Formatted text report
    """
    lines = [
        "=" * 60,
        f"JOURNAL MIGRATION COMPARISON REPORT",
        f"Tenant: {report['tenant_name']} ({report['tenant_id']})",
        "=" * 60,
        "",
        "JOURNAL COUNTS:",
        f"  Old Pipeline: {report['journal_counts']['old_pipeline']} journals",
        f"  New Pipeline: {report['journal_counts']['new_pipeline']} journals",
        "",
        "ACCOUNT TOTALS:",
        f"  Matching:      {report['total_comparison']['summary']['matching_count']}",
        f"  Discrepancies: {report['total_comparison']['summary']['discrepancy_count']}",
        f"  Old Only:      {report['total_comparison']['summary']['old_only_count']}",
        f"  New Only:      {report['total_comparison']['summary']['new_only_count']}",
        "",
    ]
    
    if report['total_comparison']['discrepancies']:
        lines.append("DISCREPANCY DETAILS:")
        for d in report['total_comparison']['discrepancies'][:20]:
            lines.append(
                f"  {d['account_code']} ({d['name'][:30]}): "
                f"Old={d['old_total']}, New={d['new_total']}, "
                f"Diff={d['difference']}"
            )
    
    lines.extend([
        "",
        "=" * 60,
        f"OVERALL STATUS: {'MATCHING' if report['is_matching'] else 'DISCREPANCIES FOUND'}",
        "=" * 60,
    ])
    
    return "\n".join(lines)


def run_and_print_comparison(tenant_id):
    """
    Run comparison and print results to console.
    
    Args:
        tenant_id: Xero tenant ID
    """
    report = generate_report(tenant_id, output_format='text')
    print(report)
    return report
