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
  Xero allows 60 calls/minute and (on this tenant's tier) 1,000/day. Every call
  is paced by the shared RateLimitedCaller, the sweep stops at a call budget
  and at a day-allowance floor, and a minute-window 429 is waited out rather
  than skipped past. The previous 'sleep 1.1s after every 50 calls' was a
  ~2,700/min burst against a 60/min limit.

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
import logging
from datetime import date as date_type, datetime
from decimal import Decimal, InvalidOperation

from apps.xero.xero_core.exceptions import DailyLimitReached, TenantReauthRequired
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroContacts
from apps.xero.xero_data.models import AgedPayable, AgedReceivable
from apps.xero.xero_data.services import _get_credentials_for_tenant
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi, serialize_model
from apps.xero.xero_core.throttle import (
    DEFAULT_CALLS_PER_MINUTE,
    DEFAULT_HEADROOM,
    HeadroomFloorReached,
    RateLimitedCaller,
)

logger = logging.getLogger(__name__)

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
    """Build an authenticated AccountingApi, with the client that tracks budget.

    Returns (accounting_api, xero_api_client). The second is the only object
    carrying day_limit_remaining — reading it off the raw AccountingApi gives
    the xero_python client instead, so the headroom check silently never fires.
    """
    credentials = _get_credentials_for_tenant(tenant.tenant_id)
    api_client = XeroApiClient(credentials.user, tenant.tenant_id)
    accounting = XeroAccountingApi(api_client, tenant.tenant_id)
    return accounting.api_client, api_client


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


def _sync_aged(
    tenant: XeroTenant,
    *,
    kind: str,
    contact_flag: str,
    fetch_name: str,
    model,
    report_date: date_type | None = None,
    max_api_calls: int | None = None,
    calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE,
    headroom: int | None = DEFAULT_HEADROOM,
) -> dict:
    """One contact-by-contact aged-report sweep, used for payables and receivables.

    This endpoint has no bulk form: it is one API call per contact, so a tenant
    with hundreds of contacts can spend a large share of the daily allowance in
    a single sweep. Three things keep that bounded, and all three were missing
    on 28 Aug 2026 when one run spent ~83% of the day's budget and reported
    success anyway:

      - every call is paced under the per-minute limit, and a minute-window 429
        is waited out rather than counted as a per-contact error and skipped
        past (which kept issuing calls that could not succeed);
      - the sweep stops at ``max_api_calls`` and at the day-allowance floor,
        reporting where it stopped instead of running the list to the end;
      - errors are returned for the caller to judge. A sweep that wrote nothing
        must not be reported as a success.

    Returns created/updated/skipped/errors, contact_count, contacts_processed,
    api_calls, stopped_early (None | 'max-api-calls' | 'daily-limit' |
    'headroom-floor' | 'reauth-required') and completed_at.
    """
    if report_date is None:
        report_date = date_type.today()

    stats = {
        'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0,
        'contacts_processed': 0, 'api_calls': 0, 'stopped_early': None,
    }

    contacts = list(_contacts_for_tenant(tenant, contact_flag))
    stats['contact_count'] = len(contacts)
    if not contacts:
        stats['completed_at'] = datetime.utcnow().isoformat() + 'Z'
        return stats

    api, budget_client = _get_api(tenant)
    call = RateLimitedCaller(
        calls_per_minute=calls_per_minute,
        api_client=budget_client,
        headroom=headroom,
    )
    fetch = getattr(api, fetch_name)

    for contact in contacts:
        if max_api_calls is not None and call.calls >= max_api_calls:
            stats['stopped_early'] = 'max-api-calls'
            logger.info(
                'Aged %s: stopping at the %d-call budget with %d of %d contacts done.',
                kind, max_api_calls, stats['contacts_processed'], len(contacts),
            )
            break

        try:
            raw = call(fetch, tenant.tenant_id, contact.contacts_id, date=report_date)
            serialized = serialize_model(raw)
        except DailyLimitReached:
            # One call per contact: walking the rest would issue hundreds of
            # calls that cannot succeed. Stop and say so.
            stats['stopped_early'] = 'daily-limit'
            break
        except HeadroomFloorReached as exc:
            stats['stopped_early'] = 'headroom-floor'
            logger.info('Aged %s: %s', kind, exc)
            break
        except TenantReauthRequired:
            stats['stopped_early'] = 'reauth-required'
            raise
        except Exception as exc:
            logger.error(
                'Aged %s API error for contact %s (%s): %s',
                kind, contact.contacts_id, contact.name, exc,
            )
            stats['errors'] += 1
            stats['contacts_processed'] += 1
            continue

        stats['contacts_processed'] += 1

        reports_list = serialized.get('Reports', [])
        if not reports_list:
            stats['skipped'] += 1
            continue

        rows = reports_list[0].get('Rows', [])
        summary_cells = _find_summary_row(rows)
        if summary_cells is None:
            # Empty report — no aged balance for this contact.
            stats['skipped'] += 1
            continue

        buckets = _extract_buckets(summary_cells)
        if buckets is None:
            logger.warning(
                'Aged %s: unexpected cell count for contact %s, skipping.',
                kind, contact.contacts_id,
            )
            stats['skipped'] += 1
            continue

        # All buckets zero → nothing owed → skip (don't clutter the table).
        if all(value == Decimal('0') for value in buckets.values()):
            stats['skipped'] += 1
            continue

        _, created = model.objects.update_or_create(
            tenant=tenant,
            contact_id=contact.contacts_id,
            report_date=report_date,
            defaults={'contact_name': contact.name or '', **buckets},
        )
        stats['created' if created else 'updated'] += 1

    stats['api_calls'] = call.calls
    stats['completed_at'] = datetime.utcnow().isoformat() + 'Z'
    logger.info('Aged %s sync complete for tenant %s: %s', kind, tenant.tenant_id, stats)
    return stats


def sync_aged_payables(tenant: XeroTenant, report_date: date_type | None = None, **kwargs) -> dict:
    """Aged payables per supplier contact. See _sync_aged for the guard rails."""
    return _sync_aged(
        tenant,
        kind='payables',
        contact_flag='IsSupplier',
        fetch_name='get_report_aged_payables_by_contact',
        model=AgedPayable,
        report_date=report_date,
        **kwargs,
    )


def sync_aged_receivables(tenant: XeroTenant, report_date: date_type | None = None, **kwargs) -> dict:
    """Aged receivables per customer contact. See _sync_aged for the guard rails."""
    return _sync_aged(
        tenant,
        kind='receivables',
        contact_flag='IsCustomer',
        fetch_name='get_report_aged_receivables_by_contact',
        model=AgedReceivable,
        report_date=report_date,
        **kwargs,
    )
