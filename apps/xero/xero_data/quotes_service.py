"""
Xero Quotes sync service.

Strategy:
  - List quotes via `api.get_quotes(tenant_id, if_modified_since=..., page=N)`.
    Xero pages 100 per request.
  - For each quote in the list response, the line items are summary-only.
    To get full line items we call `api.get_quote(tenant_id, quote_id)`.
  - Upsert the header; delete-and-recreate line items (matches XeroJournals pattern).
  - Soft-delete: Xero Status="DELETED" sets local status='DELETED' but keeps the
    row + lines (audit trail).

Rate limiting:
  Xero allows 60 calls/min. Detail-per-quote is one extra call per quote. We
  sleep 1.1s after every 50 calls. For incremental syncs (most days) this is
  noop; full backfills throttle correctly.

Incremental cursor:
  We persist nothing yet — caller passes `modified_since` (a datetime). A
  follow-up can wire this to `XeroLastUpdate` once Quotes is in production.

Daily budget:
  The Klikk tenant is capped at 1,000 Xero calls/day and the nightly pipeline
  must stay well under that (2026-08-18 budget blowout). The per-quote detail
  call is what makes this sync expensive — a full backfill of N quotes costs
  N + ceil(N/100) calls. `sync_xero_quotes(max_api_calls=N)` is a hard cap on
  calls per run: list pages are counted honestly (+1 per page), and detail
  fetching stops the moment the cap is reached. Whatever was fetched before
  that is upserted; the remainder is left for the next run.
"""
import logging
import time
from datetime import date as date_type, datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable

from django.db import transaction

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import (
    XeroQuote, XeroQuoteLineItem, XeroQuoteStatus,
)
from apps.xero.xero_data.services import _get_credentials_for_tenant
from apps.xero.xero_core.services import (
    XeroApiClient, XeroAccountingApi, serialize_model,
)
from apps.xero.xero_metadata.models import XeroContacts, XeroAccount, XeroTracking

logger = logging.getLogger(__name__)

_RATE_LIMIT_BATCH = 50
_RATE_LIMIT_SLEEP = 1.1


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

import re

# Xero often returns the .NET legacy format "/Date(1719705600000+0000)/"
_XERO_DOTNET_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")

def _parse_xero_dotnet_dt(s):
    m = _XERO_DOTNET_RE.match(s)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt_timezone.utc)


def _parse_decimal(value, default=Decimal('0')):
    if value is None or value == '':
        return default
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return default


def _parse_date(value):
    """Xero often returns '/Date(1234567890+0000)/' style strings — let
    the SDK's serialize_model already convert these to ISO strings."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_type):
        return value
    s = str(value)
    if s.startswith('/Date('):
        dt = _parse_xero_dotnet_dt(s)
        return dt.date() if dt else None
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d', '%Y-%m-%dT%H:%M:%S.%f%z',
                '%Y-%m-%dT%H:%M:%S.%f'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    if s.startswith('/Date('):
        return _parse_xero_dotnet_dt(s)
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _normalise_status(raw):
    if not raw:
        return XeroQuoteStatus.DRAFT
    up = str(raw).upper()
    valid = {s.value for s in XeroQuoteStatus}
    return up if up in valid else XeroQuoteStatus.DRAFT


# ---------------------------------------------------------------------------
# API plumbing
# ---------------------------------------------------------------------------

def _get_api(tenant: XeroTenant):
    credentials = _get_credentials_for_tenant(tenant.tenant_id)
    api_client = XeroApiClient(credentials.user, tenant.tenant_id)
    accounting = XeroAccountingApi(api_client, tenant.tenant_id)
    return accounting.api_client


def _iter_quote_pages(api, tenant_id, modified_since=None, stats=None,
                      max_api_calls=None):
    """Yield list-shape Quote dicts page by page. Stops when a page is short.

    The generator owns the page loop, so it owns the honest page counter
    (`stats['api_calls']` +1 per page actually requested — not the old
    per-quote 1/100 float estimate) and the budget guard: if `max_api_calls`
    is already reached before a further page is needed, it sets
    `stats['budget_exhausted'] = True` and returns what it has.
    """
    if stats is None:
        stats = {'api_calls': 0}
    page = 1
    while True:
        if max_api_calls is not None and stats['api_calls'] >= max_api_calls:
            stats['budget_exhausted'] = True
            logger.warning(
                'Quotes sync: API budget exhausted (%d/%d calls) before list '
                'page %d — stopping list pass',
                stats['api_calls'], max_api_calls, page,
            )
            return
        kwargs = {'page': page}
        if modified_since:
            kwargs['if_modified_since'] = modified_since
        # Count the attempt, not the success: a call that 429s/401s still
        # spent budget against Xero's daily cap.
        stats['api_calls'] += 1
        raw = api.get_quotes(tenant_id, **kwargs)
        ser = serialize_model(raw)
        quotes = ser.get('Quotes', []) or []
        if not quotes:
            return
        for q in quotes:
            yield q
        # Xero pages 100; short page means last page.
        if len(quotes) < 100:
            return
        page += 1


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _resolve_contact(tenant: XeroTenant, contact_dict):
    if not contact_dict:
        return None, '', ''
    contact_id = contact_dict.get('ContactID') or ''
    name = contact_dict.get('Name') or ''
    contact_row = None
    if contact_id:
        contact_row = XeroContacts.objects.filter(
            organisation=tenant, contacts_id=contact_id
        ).first()
    return contact_row, contact_id, name


def _resolve_account(tenant: XeroTenant, account_code):
    if not account_code:
        return None
    return XeroAccount.objects.filter(
        organisation=tenant, code=account_code
    ).first()


def _resolve_tracking(tenant: XeroTenant, tracking_dict):
    if not tracking_dict:
        return None
    tcid = tracking_dict.get('TrackingCategoryID')
    option = tracking_dict.get('Option')
    if not tcid:
        return None
    q = XeroTracking.objects.filter(
        organisation=tenant, tracking_category_id=tcid
    )
    if option:
        q = q.filter(option=option)
    return q.first()


def _upsert_quote(tenant: XeroTenant, detail: dict) -> tuple[XeroQuote, bool]:
    """Upsert a single quote (detail shape — already has full line items)."""
    quote_id = detail.get('QuoteID')
    if not quote_id:
        raise ValueError('Quote payload missing QuoteID')

    contact_row, contact_id_str, contact_name = _resolve_contact(
        tenant, detail.get('Contact'))

    defaults = {
        'quote_number': detail.get('QuoteNumber') or '',
        'reference': detail.get('Reference') or '',
        'contact': contact_row,
        'xero_contact_id': contact_id_str,
        'contact_name': contact_name,
        'status': _normalise_status(detail.get('Status')),
        'date': _parse_date(detail.get('Date')),
        'expiry_date': _parse_date(detail.get('ExpiryDate')),
        'currency_code': detail.get('CurrencyCode') or '',
        'currency_rate': _parse_decimal(detail.get('CurrencyRate'), Decimal('1')),
        'sub_total': _parse_decimal(detail.get('SubTotal')),
        'total_tax': _parse_decimal(detail.get('TotalTax')),
        'total': _parse_decimal(detail.get('Total')),
        'total_discount': _parse_decimal(detail.get('TotalDiscount'), None),
        'title': detail.get('Title') or '',
        'summary': detail.get('Summary') or '',
        'terms': detail.get('Terms') or '',
        'line_amount_types': detail.get('LineAmountTypes') or '',
        'branding_theme_id': detail.get('BrandingThemeID') or '',
        'updated_date_utc': _parse_datetime(detail.get('UpdatedDateUTC')),
        'collection': detail,
    }
    obj, created = XeroQuote.objects.update_or_create(
        organisation=tenant,
        quote_id=quote_id,
        defaults=defaults,
    )
    return obj, created


def _replace_line_items(quote: XeroQuote, detail: dict, tenant: XeroTenant):
    """Wipe + recreate line items for this quote — simpler than diffing."""
    XeroQuoteLineItem.objects.filter(quote=quote).delete()
    line_items_raw = detail.get('LineItems') or []
    objs = []
    for idx, li in enumerate(line_items_raw):
        account_code = li.get('AccountCode') or ''
        account_row = _resolve_account(tenant, account_code)
        tracking_list = li.get('Tracking') or []
        t1 = _resolve_tracking(tenant, tracking_list[0]) if len(tracking_list) >= 1 else None
        t2 = _resolve_tracking(tenant, tracking_list[1]) if len(tracking_list) >= 2 else None
        objs.append(XeroQuoteLineItem(
            quote=quote,
            line_item_id=li.get('LineItemID') or '',
            description=li.get('Description') or '',
            quantity=_parse_decimal(li.get('Quantity')),
            unit_amount=_parse_decimal(li.get('UnitAmount')),
            item_code=li.get('ItemCode') or '',
            account_code=account_code,
            account=account_row,
            tax_type=li.get('TaxType') or '',
            tax_amount=_parse_decimal(li.get('TaxAmount')),
            line_amount=_parse_decimal(li.get('LineAmount')),
            discount_rate=_parse_decimal(li.get('DiscountRate'), None),
            discount_amount=_parse_decimal(li.get('DiscountAmount'), None),
            tracking1=t1,
            tracking2=t2,
            position=idx,
        ))
    if objs:
        XeroQuoteLineItem.objects.bulk_create(objs)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def sync_xero_quotes(tenant: XeroTenant, modified_since: datetime | None = None,
                     full: bool = False,
                     max_api_calls: int | None = None) -> dict:
    """
    Incremental (or full) sync of Quotes for one tenant.

    Args:
        modified_since: only sync quotes updated since (Xero If-Modified-Since)
        full: ignore modified_since
        max_api_calls: hard cap on Xero API calls (list pages + per-quote
            detail calls) for this run. None (default) = unlimited, i.e. the
            pre-existing behaviour for the console view and the management
            command. When set, no further detail call (or list page) is
            issued once the cap is reached; quotes already fetched are still
            upserted and the rest are left for the next run. Exists so the
            nightly pipeline can run this inside a fixed per-tenant budget
            (Klikk tenant: 1,000 calls/day).

    Returns:
        {
          'created': int,
          'updated': int,
          'line_items_total': int,
          'errors': int,
          'quote_count': int,         # quotes listed (not necessarily fetched)
          'api_calls': int,           # honest count: pages + detail calls
          'budget_exhausted': bool,   # always present; True only if the cap
                                      # stopped work with quotes still unfetched
          'quotes_unfetched': int,    # present only when budget_exhausted
          'completed_at': ISO-8601 str,
        }
    """
    stats = {
        'created': 0, 'updated': 0, 'line_items_total': 0,
        'errors': 0, 'quote_count': 0, 'api_calls': 0,
        'budget_exhausted': False,
    }

    if full:
        modified_since = None
    api = _get_api(tenant)

    # First pass: collect quote IDs from the list endpoint. The generator
    # counts pages (+1 per page requested) and honours the budget itself.
    quote_ids: list[str] = []
    try:
        for summary in _iter_quote_pages(api, tenant.tenant_id,
                                         modified_since=modified_since,
                                         stats=stats,
                                         max_api_calls=max_api_calls):
            qid = summary.get('QuoteID')
            if qid:
                quote_ids.append(qid)
    except Exception as exc:
        logger.exception('Quotes list call failed for tenant %s', tenant.tenant_id)
        stats['errors'] += 1
        stats['completed_at'] = datetime.now(dt_timezone.utc).isoformat()
        stats['error_message'] = str(exc)
        return stats

    stats['quote_count'] = len(quote_ids)

    if not quote_ids:
        stats['completed_at'] = datetime.now(dt_timezone.utc).isoformat()
        return stats

    # Second pass: per-quote detail call for full line items. This is the
    # expensive step — one call per quote — so the budget is checked before
    # every call, and we stop cleanly (keeping what we have) when it is spent.
    for idx, qid in enumerate(quote_ids, start=1):
        if max_api_calls is not None and stats['api_calls'] >= max_api_calls:
            unfetched = len(quote_ids) - (idx - 1)
            stats['budget_exhausted'] = True
            stats['quotes_unfetched'] = unfetched
            logger.warning(
                'Quotes sync: API budget exhausted (%d/%d calls) — %d of %d '
                'quotes left unfetched for tenant %s; synced rows are kept',
                stats['api_calls'], max_api_calls, unfetched, len(quote_ids),
                tenant.tenant_id,
            )
            break

        if idx > 1 and (idx - 1) % _RATE_LIMIT_BATCH == 0:
            logger.info('Quotes sync: pausing after %d API calls', idx - 1)
            time.sleep(_RATE_LIMIT_SLEEP)

        try:
            stats['api_calls'] += 1  # count the attempt (see list pass)
            raw = api.get_quote(tenant.tenant_id, qid)
            ser = serialize_model(raw)
            details = (ser.get('Quotes') or [ser]) if isinstance(ser, dict) else []
            if not details:
                stats['errors'] += 1
                continue
            detail = details[0]
        except Exception as exc:
            logger.error('Quote detail fetch failed for %s: %s', qid, exc)
            stats['errors'] += 1
            continue

        try:
            with transaction.atomic():
                quote, created = _upsert_quote(tenant, detail)
                _replace_line_items(quote, detail, tenant)
            if created:
                stats['created'] += 1
            else:
                stats['updated'] += 1
            stats['line_items_total'] += len(detail.get('LineItems') or [])
        except Exception as exc:
            logger.exception('Quote upsert failed for %s', qid)
            stats['errors'] += 1

    stats['completed_at'] = datetime.now(dt_timezone.utc).isoformat()
    logger.info('Quotes sync for tenant %s: %s', tenant.tenant_id, stats)
    return stats
