"""
Xero Invoice sync service — parallel to XeroTransactionSource.

Design constraint: the existing XeroTransactionSource.collection JSON path
remains untouched. Trial balance still computes from XeroJournals
(xero_cube/services.py). These new tables are pure denormalisation for
fast querying + MCP exposure.

Xero's get_invoices() returns full line items inline (unlike Quotes which
needs a per-quote detail call) — single pass, no detail round-trip.

Rate limit: 60/min budget. We sleep 1.1s every 50 calls — defensive against
multi-tenant sweeps; trivial overhead on single-tenant runs.

Daily budget: the Klikk tenant is capped at 1,000 Xero calls/day and the
nightly pipeline must stay well under that (2026-08-18 budget blowout).
`sync_xero_invoices(max_api_calls=N)` is a hard cap on list pages requested
in one run — the page generator owns the counter and refuses to request
another page once the cap is reached; rows already fetched are still upserted.
"""
import logging
import time
from datetime import date as date_type, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
import re

from django.db import transaction

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import (
    XeroInvoice, XeroInvoiceLineItem, XeroInvoiceStatus, XeroInvoiceType,
)
from apps.xero.xero_data.services import _get_credentials_for_tenant
from apps.xero.xero_core.services import (
    XeroApiClient, XeroAccountingApi, serialize_model,
)
from apps.xero.xero_metadata.models import XeroContacts, XeroAccount, XeroTracking

logger = logging.getLogger(__name__)

_RATE_LIMIT_BATCH = 50
_RATE_LIMIT_SLEEP = 1.1

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
                '%Y-%m-%d'):
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
        return XeroInvoiceStatus.DRAFT
    up = str(raw).upper()
    valid = {s.value for s in XeroInvoiceStatus}
    return up if up in valid else XeroInvoiceStatus.DRAFT


def _normalise_type(raw):
    if not raw:
        return XeroInvoiceType.ACCREC
    up = str(raw).upper()
    return up if up in {t.value for t in XeroInvoiceType} else XeroInvoiceType.ACCREC


def _get_api(tenant: XeroTenant):
    credentials = _get_credentials_for_tenant(tenant.tenant_id)
    api_client = XeroApiClient(credentials.user, tenant.tenant_id)
    accounting = XeroAccountingApi(api_client, tenant.tenant_id)
    return accounting.api_client


def _iter_invoice_pages(api, tenant_id, modified_since=None, statuses=None,
                       invoice_type=None, stats=None, max_api_calls=None):
    """Yield invoice dicts page by page. Stops on short page.

    The generator owns the page loop, so it also owns the API-call counter
    (`stats['api_calls']`, +1 per page actually requested), the rate-limit
    pause, and the budget guard. Counting here — rather than inferring pages
    from the consumer's row count — keeps the count honest and guarantees
    no page is requested after the budget is spent.

    When `max_api_calls` is set and reached while more pages remain, sets
    `stats['budget_exhausted'] = True` and returns; rows from pages already
    fetched have all been yielded and are not discarded.
    """
    if stats is None:
        stats = {'api_calls': 0}
    page = 1
    while True:
        if max_api_calls is not None and stats['api_calls'] >= max_api_calls:
            stats['budget_exhausted'] = True
            logger.warning(
                'Invoices sync: API budget exhausted (%d/%d calls) before '
                'page %d — stopping; rows already fetched are kept',
                stats['api_calls'], max_api_calls, page,
            )
            return
        kwargs = {'page': page}
        if modified_since:
            kwargs['if_modified_since'] = modified_since
        if statuses:
            kwargs['statuses'] = statuses
        if invoice_type:
            # Use Xero's `where` query param: e.g. Type=="ACCREC"
            kwargs['where'] = f'Type=="{invoice_type}"'
        # Count the attempt, not the success: a call that 429s/401s still
        # spent budget against Xero's daily cap.
        stats['api_calls'] += 1
        raw = api.get_invoices(tenant_id, **kwargs)
        ser = serialize_model(raw)
        invoices = ser.get('Invoices', []) or []
        if not invoices:
            return
        for inv in invoices:
            yield inv
        if len(invoices) < 100:
            return
        page += 1
        if (page - 1) % _RATE_LIMIT_BATCH == 0:
            logger.info('Invoices sync: pausing after %d pages', page - 1)
            time.sleep(_RATE_LIMIT_SLEEP)


def _resolve_contact(tenant: XeroTenant, contact_dict):
    if not contact_dict:
        return None, '', ''
    cid = contact_dict.get('ContactID') or ''
    name = contact_dict.get('Name') or ''
    row = None
    if cid:
        row = XeroContacts.objects.filter(
            organisation=tenant, contacts_id=cid
        ).first()
    return row, cid, name


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


def _upsert_invoice(tenant: XeroTenant, detail: dict) -> tuple[XeroInvoice, bool]:
    invoice_id = detail.get('InvoiceID')
    if not invoice_id:
        raise ValueError('Invoice payload missing InvoiceID')

    contact_row, contact_id_str, contact_name = _resolve_contact(
        tenant, detail.get('Contact'))

    defaults = {
        'invoice_number': detail.get('InvoiceNumber') or '',
        'reference': detail.get('Reference') or '',
        'type': _normalise_type(detail.get('Type')),
        'status': _normalise_status(detail.get('Status')),
        'contact': contact_row,
        'xero_contact_id': contact_id_str,
        'contact_name': contact_name,
        'date': _parse_date(detail.get('Date')),
        'due_date': _parse_date(detail.get('DueDate')),
        'fully_paid_on_date': _parse_date(detail.get('FullyPaidOnDate')),
        'expected_payment_date': _parse_date(detail.get('ExpectedPaymentDate')),
        'planned_payment_date': _parse_date(detail.get('PlannedPaymentDate')),
        'currency_code': detail.get('CurrencyCode') or '',
        'currency_rate': _parse_decimal(detail.get('CurrencyRate'), Decimal('1')),
        'sub_total': _parse_decimal(detail.get('SubTotal')),
        'total_tax': _parse_decimal(detail.get('TotalTax')),
        'total': _parse_decimal(detail.get('Total')),
        'total_discount': _parse_decimal(detail.get('TotalDiscount'), None),
        'amount_due': _parse_decimal(detail.get('AmountDue')),
        'amount_paid': _parse_decimal(detail.get('AmountPaid')),
        'amount_credited': _parse_decimal(detail.get('AmountCredited')),
        'line_amount_types': detail.get('LineAmountTypes') or '',
        'branding_theme_id': detail.get('BrandingThemeID') or '',
        'url': detail.get('Url') or '',
        'sent_to_contact': bool(detail.get('SentToContact')),
        'is_discounted': bool(detail.get('IsDiscounted')),
        'has_attachments': bool(detail.get('HasAttachments')),
        'has_errors': bool(detail.get('HasErrors')),
        'updated_date_utc': _parse_datetime(detail.get('UpdatedDateUTC')),
        'collection': detail,
    }
    obj, created = XeroInvoice.objects.update_or_create(
        organisation=tenant, invoice_id=invoice_id, defaults=defaults,
    )
    return obj, created


def _replace_line_items(invoice: XeroInvoice, detail: dict, tenant: XeroTenant):
    """Wipe + recreate line items for this invoice."""
    XeroInvoiceLineItem.objects.filter(invoice=invoice).delete()
    raw_lines = detail.get('LineItems') or []
    objs = []
    for idx, li in enumerate(raw_lines):
        account_code = li.get('AccountCode') or ''
        account_row = _resolve_account(tenant, account_code)
        trk = li.get('Tracking') or []
        t1 = _resolve_tracking(tenant, trk[0]) if len(trk) >= 1 else None
        t2 = _resolve_tracking(tenant, trk[1]) if len(trk) >= 2 else None
        objs.append(XeroInvoiceLineItem(
            invoice=invoice,
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
        XeroInvoiceLineItem.objects.bulk_create(objs)


# Overlap subtracted from the last successful run's stamp when the caller gives
# no cursor. Wide enough to absorb clock skew and a run that stamped before
# Xero finished indexing an edit; cheap because a day of changes is one page.
DEFAULT_CURSOR_OVERLAP = timedelta(days=3)


def default_modified_since(tenant: XeroTenant) -> datetime | None:
    """Cursor for a run that gave none: last successful run minus an overlap.

    Before 2026-09-03 every caller that omitted `modified_since` (the console
    button, the V2 pipeline stage, the MCP tool) silently re-pulled the whole
    invoice list — 38 Xero calls and ~45 s to rewrite 3,700 unchanged Klikk
    rows for a handful of changes. The nightly script already passed a
    lookback; this makes that the default everywhere.

    Returns None (= full sync) when the store has never been stamped, so a
    fresh tenant still loads everything. The value is a naive UTC datetime:
    it goes to Xero's If-Modified-Since header, not the ORM, and the SDK
    reformats an aware value.
    """
    from apps.xero.xero_sync.models import XeroLastUpdate
    stamp = XeroLastUpdate.objects.filter(
        end_point='invoice_store', organisation=tenant,
    ).values_list('date', flat=True).first()
    if not stamp:
        return None
    return stamp.astimezone(dt_timezone.utc).replace(tzinfo=None) - DEFAULT_CURSOR_OVERLAP


def sync_xero_invoices(tenant: XeroTenant,
                       modified_since: datetime | None = None,
                       statuses: list | None = None,
                       invoice_type: str | None = None,
                       full: bool = False,
                       max_api_calls: int | None = None) -> dict:
    """
    Sync invoices for one tenant.

    Args:
        modified_since: only sync invoices updated since (Xero If-Modified-Since).
            None = derive from the last successful run (see default_modified_since);
            a tenant never synced before falls back to a full pull.
        statuses: optional list of XeroInvoiceStatus values to filter
        invoice_type: optional 'ACCREC' or 'ACCPAY'
        full: ignore modified_since and pull everything (deleted/voided
            invoices do not surface through If-Modified-Since, so keep an
            occasional full run — the weekly cron already does one)
        max_api_calls: hard cap on Xero list-page calls for this run.
            None (default) = unlimited, i.e. the pre-existing behaviour for
            the console view and the management command. When set, no page
            is requested once the cap is reached; rows already fetched are
            still upserted. Exists so the nightly pipeline can run this
            inside a fixed per-tenant budget (Klikk tenant: 1,000 calls/day).

    Returns stats dict. Always contains 'budget_exhausted' (bool) — True only
    when the cap stopped paging while more pages remained — so callers can
    branch on it directly.

    On success records XeroLastUpdate['invoice_store'] so the console can
    show an honest "last run" for this store. Skipped when the list call
    itself failed — a failed run must not claim freshness.
    """
    stats = {
        'created': 0, 'updated': 0, 'line_items_total': 0,
        'errors': 0, 'invoice_count': 0, 'api_calls': 0,
        'budget_exhausted': False,
        # Measured coverage: the months of the invoices this run actually
        # wrote, by XeroInvoice.date — the same field the stage's source
        # evidence is scoped by (xero_source_evidence._SOURCES), so run
        # evidence and source evidence can never disagree about which clock
        # this stage runs on. The V2 run record replaces its requested
        # periods with this on success; a run request itself must never
        # carry a coverage claim (30 Aug 2026).
        'affected_periods': [],
    }
    affected_months = set()

    if full:
        modified_since = None
    elif modified_since is None:
        modified_since = default_modified_since(tenant)
        if modified_since is not None:
            print(f'[INVOICES] No cursor given; syncing invoices modified since '
                  f'{modified_since:%Y-%m-%d %H:%M} UTC (last run minus overlap)')
    api = _get_api(tenant)

    list_call_failed = False
    try:
        # The generator owns the page loop and therefore the api_calls
        # counter, the rate-limit pause, and the budget guard (see its docstring).
        for inv in _iter_invoice_pages(api, tenant.tenant_id,
                                       modified_since=modified_since,
                                       statuses=statuses,
                                       invoice_type=invoice_type,
                                       stats=stats,
                                       max_api_calls=max_api_calls):
            stats['invoice_count'] += 1

            try:
                with transaction.atomic():
                    invoice, created = _upsert_invoice(tenant, inv)
                    _replace_line_items(invoice, inv, tenant)
                if created:
                    stats['created'] += 1
                else:
                    stats['updated'] += 1
                stats['line_items_total'] += len(inv.get('LineItems') or [])
                # Only rows actually written count as coverage; a dateless
                # invoice has no month to claim and claims none.
                if invoice.date:
                    affected_months.add(f'{invoice.date.year:04d}-{invoice.date.month:02d}')
            except Exception:
                logger.exception('Invoice upsert failed for %s', inv.get('InvoiceID'))
                stats['errors'] += 1
    except Exception as exc:
        logger.exception('Invoices list call failed for tenant %s', tenant.tenant_id)
        stats['errors'] += 1
        stats['error_message'] = str(exc)
        list_call_failed = True

    if not list_call_failed:
        # Honest "last run" stamp for the console Processes page.
        #
        # The key is 'invoice_store', NOT 'invoices'. 'invoices' is the
        # transactions-sync If-Modified-Since cursor owned by XeroApiClient
        # (apps/xero/xero_core/services.py); writing it here would advance that
        # cursor without the transactions sync having run, and it would then
        # silently skip data. Individual row upsert failures do not block the
        # stamp — the run itself completed; only a failed list call does.
        from apps.xero.xero_sync.models import XeroLastUpdate
        XeroLastUpdate.objects.update_or_create_timestamp('invoice_store', tenant)

    stats['affected_periods'] = sorted(affected_months)
    stats['completed_at'] = datetime.now(dt_timezone.utc).isoformat()
    logger.info('Invoices sync for tenant %s: %s', tenant.tenant_id, stats)
    return stats
