"""
Aged-report sync service — AgedPayablesByContact / AgedReceivablesByContact.

Strategy:
  - Iterate over all XeroContacts for the tenant.
  - For payables: all contacts that Xero has marked IsSupplier=True in their
    collection JSON. If that flag is absent (old sync), fall through and call
    every contact — Xero returns an empty report for non-suppliers, which we skip.
  - For receivables: same logic with IsCustomer.
  - Call the per-contact endpoint, parse the six-bucket header row, upsert.

Rate limiting:
  Xero allows 60 calls/minute. For tenants with hundreds of contacts we add a
  1-second sleep after every 50 calls — well within the budget but prevents
  burst spikes that would error on Xero's per-minute window.

Response shape (serialized ReportWithRows → reports[0]):
  {
    "ReportID": "AgedPayablesByContact",
    "ReportName": "...",
    "ReportDate": "31 May 2025",
    "Rows": [
      { "RowType": "Header", "Cells": [
          {"Value": "Date"}, {"Value": "Current"}, {"Value": "1 Month"},
          {"Value": "2 Months"}, {"Value": "3 Months"}, {"Value": "Older"},
          {"Value": "Total"}
      ]},
      { "RowType": "Row", "Cells": [
          {"Value": "..."}, {"Value": "0.00"}, ...
      ]},
      { "RowType": "SummaryRow", "Cells": [...] }
    ]
  }

The SummaryRow contains the totals for the contact. We store only the summary
row (one row per contact per date). If there is no SummaryRow (empty contact),
we skip to avoid storing zero-rows for every contact.
"""
import time
import logging
from datetime import date as date_type, datetime
from decimal import Decimal, InvalidOperation

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroContacts
from apps.xero.xero_data.models import AgedPayable, AgedReceivable
from apps.xero.xero_data.services import _get_credentials_for_tenant
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi, serialize_model

logger = logging.getLogger(__name__)

# Sleep after this many API calls to respect the 60 calls/minute rate limit
_RATE_LIMIT_BATCH = 50
_RATE_LIMIT_SLEEP = 1.1  # seconds


def _parse_decimal(value):
    """Parse a cell value string to Decimal. Returns Decimal(0) on any error."""
    if value is None:
        return Decimal('0')
    try:
        return Decimal(str(value).replace(',', '').strip() or '0')
    except (InvalidOperation, ValueError):
        return Decimal('0')


def _find_summary_row(rows):
    """
    Given the serialized Rows list from a ReportWithRows report,
    return the first SummaryRow's cells list, or None if absent.
    """
    for row in rows:
        if row.get('RowType') == 'SummaryRow':
            return row.get('Cells', [])
    return None


def _extract_buckets(cells):
    """
    Map the 7-column SummaryRow cells to bucket names.

    Expected column order (from Xero docs + header row):
      0: label (e.g. "Total")
      1: Current
      2: 1 Month
      3: 2 Months
      4: 3 Months
      5: Older
      6: Total
    """
    if not cells or len(cells) < 7:
        return None
    return {
        'current':     _parse_decimal(cells[1].get('Value')),
        'one_month':   _parse_decimal(cells[2].get('Value')),
        'two_months':  _parse_decimal(cells[3].get('Value')),
        'three_months': _parse_decimal(cells[4].get('Value')),
        'older':       _parse_decimal(cells[5].get('Value')),
        'total':       _parse_decimal(cells[6].get('Value')),
    }


def _get_api(tenant: XeroTenant):
    """Build an authenticated AccountingApi for the tenant."""
    credentials = _get_credentials_for_tenant(tenant.tenant_id)
    api_client = XeroApiClient(credentials.user, tenant.tenant_id)
    accounting = XeroAccountingApi(api_client, tenant.tenant_id)
    return accounting.api_client  # the raw AccountingApi instance


def _contacts_for_tenant(tenant: XeroTenant, flag: str):
    """
    Return a queryset of XeroContacts for the tenant.

    If the Xero collection JSON has 'IsSupplier' / 'IsCustomer' flags, filter
    to only those set to True (avoids unnecessary API calls and skips for
    contacts that won't have data).

    Falls back to ALL contacts if none have the flag set — a safe degradation
    that just results in empty-report skips on Xero's side.
    """
    contacts = XeroContacts.objects.filter(organisation=tenant)
    # Try to filter by the Xero flag
    flagged = contacts.filter(**{f'collection__{flag}': True})
    if flagged.exists():
        return flagged
    # Flag not populated — return all (Xero will just return empty reports)
    logger.warning(
        'Contacts for tenant %s do not have %s flag — will call all %d contacts.',
        tenant.tenant_id, flag, contacts.count(),
    )
    return contacts


def sync_aged_payables(tenant: XeroTenant, report_date: date_type | None = None) -> dict:
    """
    Iterate over supplier contacts, fetch aged payables per contact, upsert rows.

    Returns:
        {
          'created': int,
          'updated': int,
          'skipped': int,   # contacts with no payables data (empty report)
          'errors': int,
          'contact_count': int,
          'completed_at': str (ISO-8601),
        }
    """
    if report_date is None:
        report_date = date_type.today()

    stats = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0}

    contacts = list(_contacts_for_tenant(tenant, 'IsSupplier'))
    stats['contact_count'] = len(contacts)

    if not contacts:
        stats['completed_at'] = datetime.utcnow().isoformat() + 'Z'
        return stats

    api = _get_api(tenant)

    for idx, contact in enumerate(contacts, start=1):
        # Rate-limit: sleep after every N calls
        if idx > 1 and (idx - 1) % _RATE_LIMIT_BATCH == 0:
            logger.info('Aged payables: pausing after %d calls to respect rate limit.', idx - 1)
            time.sleep(_RATE_LIMIT_SLEEP)

        try:
            raw = api.get_report_aged_payables_by_contact(
                tenant.tenant_id,
                contact.contacts_id,
                date=report_date,
            )
            serialized = serialize_model(raw)
        except Exception as exc:
            logger.error(
                'Aged payables API error for contact %s (%s): %s',
                contact.contacts_id, contact.name, exc,
            )
            stats['errors'] += 1
            continue

        # Navigate: Reports[0].Rows
        reports_list = serialized.get('Reports', [])
        if not reports_list:
            stats['skipped'] += 1
            continue

        report = reports_list[0]
        rows = report.get('Rows', [])
        summary_cells = _find_summary_row(rows)

        if summary_cells is None:
            # Empty report — no payables for this contact
            stats['skipped'] += 1
            continue

        buckets = _extract_buckets(summary_cells)
        if buckets is None:
            logger.warning(
                'Aged payables: unexpected cell count for contact %s, skipping.',
                contact.contacts_id,
            )
            stats['skipped'] += 1
            continue

        # All buckets zero → contact has no payables → skip (don't clutter DB)
        if all(v == Decimal('0') for v in buckets.values()):
            stats['skipped'] += 1
            continue

        obj, created = AgedPayable.objects.update_or_create(
            tenant=tenant,
            contact_id=contact.contacts_id,
            report_date=report_date,
            defaults={
                'contact_name': contact.name or '',
                **buckets,
            },
        )
        if created:
            stats['created'] += 1
        else:
            stats['updated'] += 1

    stats['completed_at'] = datetime.utcnow().isoformat() + 'Z'
    logger.info(
        'Aged payables sync complete for tenant %s: %s',
        tenant.tenant_id, stats,
    )
    return stats


def sync_aged_receivables(tenant: XeroTenant, report_date: date_type | None = None) -> dict:
    """
    Iterate over customer contacts, fetch aged receivables per contact, upsert rows.

    Returns same shape as sync_aged_payables.
    """
    if report_date is None:
        report_date = date_type.today()

    stats = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0}

    contacts = list(_contacts_for_tenant(tenant, 'IsCustomer'))
    stats['contact_count'] = len(contacts)

    if not contacts:
        stats['completed_at'] = datetime.utcnow().isoformat() + 'Z'
        return stats

    api = _get_api(tenant)

    for idx, contact in enumerate(contacts, start=1):
        if idx > 1 and (idx - 1) % _RATE_LIMIT_BATCH == 0:
            logger.info('Aged receivables: pausing after %d calls to respect rate limit.', idx - 1)
            time.sleep(_RATE_LIMIT_SLEEP)

        try:
            raw = api.get_report_aged_receivables_by_contact(
                tenant.tenant_id,
                contact.contacts_id,
                date=report_date,
            )
            serialized = serialize_model(raw)
        except Exception as exc:
            logger.error(
                'Aged receivables API error for contact %s (%s): %s',
                contact.contacts_id, contact.name, exc,
            )
            stats['errors'] += 1
            continue

        reports_list = serialized.get('Reports', [])
        if not reports_list:
            stats['skipped'] += 1
            continue

        report = reports_list[0]
        rows = report.get('Rows', [])
        summary_cells = _find_summary_row(rows)

        if summary_cells is None:
            stats['skipped'] += 1
            continue

        buckets = _extract_buckets(summary_cells)
        if buckets is None:
            logger.warning(
                'Aged receivables: unexpected cell count for contact %s, skipping.',
                contact.contacts_id,
            )
            stats['skipped'] += 1
            continue

        if all(v == Decimal('0') for v in buckets.values()):
            stats['skipped'] += 1
            continue

        obj, created = AgedReceivable.objects.update_or_create(
            tenant=tenant,
            contact_id=contact.contacts_id,
            report_date=report_date,
            defaults={
                'contact_name': contact.name or '',
                **buckets,
            },
        )
        if created:
            stats['created'] += 1
        else:
            stats['updated'] += 1

    stats['completed_at'] = datetime.utcnow().isoformat() + 'Z'
    logger.info(
        'Aged receivables sync complete for tenant %s: %s',
        tenant.tenant_id, stats,
    )
    return stats
