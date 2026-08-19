"""
Import documents (attachments) from Xero and link them to transactions in the DB.

Requires Xero OAuth scope: accounting.attachments or accounting.attachments.read.

Supported transaction types: Invoice, CreditNote, BankTransaction.

Two passes (2026-08-19 redesign — see Klikk-Xero-Code-Audit-2026-08-19.md):

1. DISCOVERY — ``discover_attachments_for_tenant``. Pages the Invoices /
   BankTransactions / CreditNotes list endpoints at pageSize=1000 (optionally
   with If-Modified-Since) and reads the ``HasAttachments`` flag Xero returns on
   every record, persisting it to ``XeroTransactionSource.has_attachments``.
   One call per 1,000 records instead of one Attachments probe per record
   (Klikk: 17,281 probes -> ~19 list pages). ``summaryOnly`` is never used
   because it strips HasAttachments on Invoices. The nightly pipeline's own
   list GETs also record the flag at ingest (see the model manager), so most
   rows are already known before this pass runs.

2. FETCH — ``sync_documents_for_tenant``. Only for rows flagged
   ``has_attachments=True`` that have no stored document (or were updated in
   Xero since ``modified_after``), call the per-object Attachments list and
   download each file. That is the irreducible cost: 1 + files calls per
   object that actually has attachments.
"""
import logging
import os
import re
import time
from datetime import datetime, timezone as dt_timezone

from django.core.files.base import ContentFile
from django.db import transaction as db_transaction
from django.db.models import Exists, OuterRef
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from xero_python.exceptions import RateLimitException

from apps.xero.xero_core.exceptions import DailyLimitReached
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import XeroTransactionSource, XeroDocument
from apps.xero.xero_data.services import _get_credentials_for_tenant
from apps.xero.xero_core.services import (
    MAX_RETRY_AFTER_SLEEP,
    XeroApiClient,
    XeroAccountingApi,
    retry_after_seconds,
    serialize_model,
)

logger = logging.getLogger(__name__)

# Transaction types that support attachments in Xero Accounting API AND are
# mirrored in XeroTransactionSource. (ManualJournals also carry HasAttachments
# but live in XeroJournalsSource; not covered here.)
SUPPORTED_SOURCE_TYPES = ('Invoice', 'CreditNote', 'BankTransaction')

# Xero tenant limits: 60 calls/min, 5000 calls/day.
DEFAULT_CALLS_PER_MINUTE = 55

# Largest page Xero accepts on Invoices / BankTransactions / CreditNotes.
DISCOVERY_PAGE_SIZE = 1000

# Default floor of X-DayLimit-Remaining below which discretionary fetch work
# stops cleanly, so the 02:45 nightly pipeline never starves. MEASURED
# 2026-08-19: the Klikk tenant's X-DayLimit-Remaining reads 999 right after the
# daily reset, i.e. the cap is 1,000/day (Xero's lower tier), not 5,000 — so the
# floor is 300 (30%); pass --headroom to override per tenant.
DEFAULT_HEADROOM = 300

_XERO_DATE_RE = re.compile(r'/Date\((\d+)(?:[+-]\d{4})?\)/')


def parse_xero_date(value):
    """Parse Xero's /Date(ms+0000)/ or an ISO-8601 string into an aware UTC datetime, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    if isinstance(value, str):
        m = _XERO_DATE_RE.search(value)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=dt_timezone.utc)
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)
        except ValueError:
            return None
    return None


# Back-compat name; the canonical exception lives in apps.xero.xero_core.exceptions.
XeroDailyLimitReached = DailyLimitReached


class HeadroomFloorReached(Exception):
    """X-DayLimit-Remaining fell below the configured floor; stop discretionary work."""

    def __init__(self, remaining, floor):
        super().__init__(
            f"Xero X-DayLimit-Remaining={remaining} is below the floor of {floor}; "
            f"stopping to preserve nightly headroom"
        )
        self.remaining = remaining
        self.floor = floor


class _RateLimitedCaller:
    """Throttles Xero API calls under the per-minute limit; waits out minute-window 429s,
    raises DailyLimitReached when the daily limit is hit, and HeadroomFloorReached when
    the tenant's remaining daily allowance (from X-DayLimit-Remaining) drops below ``headroom``."""

    def __init__(self, calls_per_minute=DEFAULT_CALLS_PER_MINUTE, api_client=None, headroom=None):
        self.min_interval = 60.0 / calls_per_minute
        self.calls = 0
        self._last_call = 0.0
        self.api_client = api_client      # XeroApiClient (exposes day_limit_remaining)
        self.headroom = headroom

    @property
    def day_limit_remaining(self):
        return getattr(self.api_client, 'day_limit_remaining', None)

    def check_headroom(self):
        remaining = self.day_limit_remaining
        if self.headroom is not None and remaining is not None and remaining < self.headroom:
            raise HeadroomFloorReached(remaining, self.headroom)

    def __call__(self, fn, *args, **kwargs):
        self.check_headroom()
        for attempt in (1, 2):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                return fn(*args, **kwargs)
            except RateLimitException as e:
                # Day-window 429s are normally converted to DailyLimitReached by
                # XeroApiClient's HTTP guard before reaching here; this branch
                # handles minute-window 429s, with the same cap as a backstop.
                problem = (e.rate_limit or '').lower()
                delay = retry_after_seconds(e)
                if problem == 'day' or delay > MAX_RETRY_AFTER_SLEEP or attempt == 2:
                    raise DailyLimitReached(
                        f"Xero rate limit ({problem or 'unknown window'}, "
                        f"Retry-After={delay}s) — aborting instead of sleeping",
                        retry_after=delay,
                    ) from e
                logger.warning("Xero per-minute rate limit hit; sleeping %ss before retry", delay)
                time.sleep(delay)


# Map our transaction_source string to (list_attachments_method, get_content_method) on AccountingApi
def _get_invoice_attachments(api, tenant_id, entity_id):
    obj = api.get_invoice_attachments(tenant_id, entity_id)
    return serialize_model(obj)

def _get_invoice_attachment_content(api, tenant_id, entity_id, attachment_id, content_type):
    return api.get_invoice_attachment_by_id(tenant_id, entity_id, attachment_id, content_type)

def _get_credit_note_attachments(api, tenant_id, entity_id):
    obj = api.get_credit_note_attachments(tenant_id, entity_id)
    return serialize_model(obj)

def _get_credit_note_attachment_content(api, tenant_id, entity_id, attachment_id, content_type):
    return api.get_credit_note_attachment_by_id(tenant_id, entity_id, attachment_id, content_type)

def _get_bank_transaction_attachments(api, tenant_id, entity_id):
    obj = api.get_bank_transaction_attachments(tenant_id, entity_id)
    return serialize_model(obj)

def _get_bank_transaction_attachment_content(api, tenant_id, entity_id, attachment_id, content_type):
    return api.get_bank_transaction_attachment_by_id(tenant_id, entity_id, attachment_id, content_type)

ATTACHMENT_GETTERS = {
    'Invoice': (_get_invoice_attachments, _get_invoice_attachment_content),
    'CreditNote': (_get_credit_note_attachments, _get_credit_note_attachment_content),
    'BankTransaction': (_get_bank_transaction_attachments, _get_bank_transaction_attachment_content),
}

# Discovery: transaction_source -> (AccountingApi list method name, response key, id key)
DISCOVERY_ENDPOINTS = {
    'Invoice': ('get_invoices', 'Invoices', 'InvoiceID'),
    'BankTransaction': ('get_bank_transactions', 'BankTransactions', 'BankTransactionID'),
    'CreditNote': ('get_credit_notes', 'CreditNotes', 'CreditNoteID'),
}


def _attachment_list_from_response(serialized):
    """Extract list of attachment dicts from serialized API response (Attachments or similar)."""
    if not serialized:
        return []
    # Response shape: {"Attachments": [{"AttachmentID": "...", "FileName": "...", "MimeType": "..."}, ...]}
    if isinstance(serialized, list):
        return serialized
    for key in ('Attachments', 'attachments'):
        if key in serialized and isinstance(serialized[key], list):
            return serialized[key]
    return []


def _content_to_bytes(content):
    """Normalize API response to bytes for saving to FileField."""
    if content is None:
        return b''
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        # xero-python saves binary responses to a temp file and returns its PATH as a str;
        # read (and clean up) the temp file instead of treating the path as content
        if os.path.isfile(content):
            with open(content, 'rb') as fh:
                data = fh.read()
            try:
                os.remove(content)
            except OSError:
                pass
            return data
        # Binary content (e.g. PDF) may be returned as str; latin-1 round-trips any byte
        return content.encode('latin-1')
    if hasattr(content, 'read'):
        out = content.read()
        return out if isinstance(out, bytes) else out.encode('latin-1')
    # OpenAPI client may return response object with .data
    if hasattr(content, 'data'):
        d = content.data
        if isinstance(d, bytes):
            return d
        if hasattr(d, 'read'):
            out = d.read()
            return out if isinstance(out, bytes) else out.encode('utf-8')
        if isinstance(d, str):
            return d.encode('latin-1')
        return bytes(d) if not isinstance(d, str) else d.encode('latin-1')
    try:
        return bytes(content)
    except TypeError:
        return str(content).encode('latin-1')


def _load_tenant(tenant_id):
    """Return (tenant, error_message). error_message is None when the tenant is usable."""
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return None, f'Tenant {tenant_id} not found'
    # Dead Xero refresh token — nothing can succeed; skip cleanly instead of
    # burning API budget on guaranteed-401 calls.
    if tenant.reauth_required:
        logger.warning('Xero document work skipped for tenant %s: awaiting re-authorization', tenant_id)
        return None, f'Tenant {tenant_id} needs Xero re-authorization; sync skipped'
    return tenant, None


def _build_api(tenant_id, user=None):
    """Return (XeroApiClient, AccountingApi) for the tenant, warning if the attachments scope looks absent."""
    credentials = _get_credentials_for_tenant(tenant_id, user)
    if not credentials.scope:
        credentials.scope = []
    scope_list = credentials.scope if isinstance(credentials.scope, list) else [credentials.scope]

    def _scope_str(x):
        if x is None:
            return ''
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            return x.get('scope', x.get('name', '')) or ''
        return str(x)

    if not any('attachments' in _scope_str(s).lower() for s in scope_list):
        logger.warning(
            "Xero credentials may not include accounting.attachments scope; attachment sync might fail. "
            "Add 'accounting.attachments' or 'accounting.attachments.read' to the app's OAuth scope."
        )
    api_client = XeroApiClient(credentials.user, tenant_id=tenant_id)
    xero_api = XeroAccountingApi(api_client, tenant_id)
    return api_client, xero_api.api_client


def seed_has_attachments_from_collection(tenant, source_types=None):
    """Zero-call discovery: copy HasAttachments out of the stored ``collection`` JSON
    (written by the nightly list GETs) into ``has_attachments`` for rows never checked.

    Returns the number of rows updated. Marks attachments_checked_at=now because the
    value did come from a Xero list response, just an earlier one.
    """
    types = source_types or list(SUPPORTED_SOURCE_TYPES)
    now = timezone.now()
    qs = XeroTransactionSource.objects.filter(
        organisation=tenant, transaction_source__in=types, has_attachments__isnull=True,
    )
    updated = 0
    for flag in (True, False):
        updated += qs.filter(collection__HasAttachments=flag).update(
            has_attachments=flag, attachments_checked_at=now,
        )
    return updated


def discover_attachments_for_tenant(tenant_id, user=None, source_types=None, modified_after=None,
                                    page_size=DISCOVERY_PAGE_SIZE, max_api_calls=None,
                                    calls_per_minute=DEFAULT_CALLS_PER_MINUTE, headroom=None,
                                    seed_from_collection=True):
    """
    Discovery pass: learn HasAttachments for every Invoice / BankTransaction / CreditNote
    from the paged list endpoints (pageSize=1000, never summaryOnly) and persist it.

    Args:
        modified_after: aware datetime -> sent as If-Modified-Since (incremental discovery).
            None -> full pass over every record of each type.
        max_api_calls: stop cleanly after this many list calls.
        headroom: stop cleanly when X-DayLimit-Remaining drops below this.
        seed_from_collection: first copy HasAttachments from stored collection JSON (0 calls).

    Returns dict: success, message, api_calls, records_seen, flagged_true, flagged_false,
        updated_rows, unknown_local (records Xero returned that are not mirrored locally),
        seeded, per_type, stopped_early, day_limit_remaining, errors.
    """
    types = [t for t in (source_types or list(SUPPORTED_SOURCE_TYPES)) if t in DISCOVERY_ENDPOINTS]
    result = {
        'success': True, 'message': '', 'api_calls': 0, 'records_seen': 0,
        'flagged_true': 0, 'flagged_false': 0, 'updated_rows': 0, 'unknown_local': 0,
        'seeded': 0, 'per_type': {}, 'stopped_early': None, 'day_limit_remaining': None,
        'errors': [],
        # IDs Xero just reported with HasAttachments=true. In incremental mode the
        # fetch pass must visit these even when the locally stored UpdatedDateUTC is
        # stale (the nightly pipeline may not have refreshed the record yet).
        'flagged_ids': [],
    }
    tenant, err = _load_tenant(tenant_id)
    if err:
        result.update(success=False, message=err)
        return result

    if seed_from_collection:
        result['seeded'] = seed_has_attachments_from_collection(tenant, types)

    api_client, api = _build_api(tenant_id, user)
    call = _RateLimitedCaller(calls_per_minute=calls_per_minute, api_client=api_client, headroom=headroom)

    stopped_early = None
    for txn_type in types:
        method_name, response_key, id_key = DISCOVERY_ENDPOINTS[txn_type]
        list_fn = getattr(api, method_name)
        per = {'api_calls': 0, 'records': 0, 'true': 0, 'false': 0, 'updated': 0, 'unknown_local': 0}
        result['per_type'][txn_type] = per
        page = 1
        while True:
            if max_api_calls is not None and call.calls >= max_api_calls:
                stopped_early = 'max-api-calls'
                break
            kwargs = {'page': page, 'page_size': page_size}
            if modified_after is not None:
                kwargs['if_modified_since'] = modified_after
            try:
                serialized = serialize_model(call(list_fn, tenant_id, **kwargs))
            except (DailyLimitReached, HeadroomFloorReached) as e:
                stopped_early = 'daily-limit' if isinstance(e, DailyLimitReached) else 'headroom-floor'
                result['errors'].append(f"{txn_type} discovery page {page}: {e}")
                break
            except Exception as e:
                result['errors'].append(f"{txn_type} discovery page {page}: {e}")
                logger.warning("Discovery list call failed for %s page %s: %s", txn_type, page, e)
                break
            per['api_calls'] += 1
            items = (serialized or {}).get(response_key) or []
            flags = {}
            for r in items:
                rid = r.get(id_key)
                flag = r.get('HasAttachments')
                if rid and isinstance(flag, bool):
                    flags[rid] = flag
            per['records'] += len(items)
            result['flagged_ids'].extend(k for k, v in flags.items() if v)
            per['true'] += sum(1 for v in flags.values() if v)
            per['false'] += sum(1 for v in flags.values() if not v)
            if flags:
                now = timezone.now()
                with db_transaction.atomic():
                    updated = 0
                    for flag in (True, False):
                        ids = [k for k, v in flags.items() if v is flag]
                        if ids:
                            updated += XeroTransactionSource.objects.filter(
                                organisation=tenant, transactions_id__in=ids,
                            ).update(has_attachments=flag, attachments_checked_at=now)
                per['updated'] += updated
                per['unknown_local'] += len(flags) - updated
            if not items or len(items) < page_size:
                break
            page += 1
        if stopped_early:
            break

    for per in result['per_type'].values():
        result['records_seen'] += per['records']
        result['flagged_true'] += per['true']
        result['flagged_false'] += per['false']
        result['updated_rows'] += per['updated']
        result['unknown_local'] += per['unknown_local']
    result['api_calls'] = call.calls
    result['stopped_early'] = stopped_early
    result['day_limit_remaining'] = call.day_limit_remaining
    result['success'] = not result['errors']
    result['message'] = (
        f"Discovery for tenant {tenant_id}: {result['records_seen']} records over {call.calls} list call(s) "
        f"(seeded {result['seeded']} from stored JSON; {result['flagged_true']} with attachments, "
        f"{result['flagged_false']} without; {result['updated_rows']} local rows updated, "
        f"{result['unknown_local']} not mirrored locally)"
    )
    if stopped_early:
        result['message'] += f"; stopped early: {stopped_early}"
    if result['day_limit_remaining'] is not None:
        result['message'] += f"; X-DayLimit-Remaining={result['day_limit_remaining']}"
    return result


def pending_fetch_queryset(tenant, source_types=None, transaction_ids=None, only_flagged=True):
    """Rows the fetch pass should visit, newest Xero Date first (FY2026 -> FY2025 -> ...).

    only_flagged=True restricts to has_attachments=True rows without a stored document
    (the post-discovery default). transaction_ids bypasses the flag filter — an explicit
    ID list is an instruction, and it is also how the pre-flight probe works.
    """
    types = source_types or list(SUPPORTED_SOURCE_TYPES)
    qs = XeroTransactionSource.objects.filter(organisation=tenant, transaction_source__in=types)
    qs = qs.annotate(has_doc=Exists(XeroDocument.objects.filter(transaction_source=OuterRef('pk'))))
    if transaction_ids is not None:
        qs = qs.filter(transactions_id__in=transaction_ids)
    elif only_flagged:
        qs = qs.filter(has_attachments=True)
    # Xero Date is '/Date(<ms>+0000)/' — fixed-width ms since epoch, so string
    # ordering == chronological ordering. Newest first keeps FY2026 ahead of FY2025.
    return qs.order_by(KeyTextTransform('Date', 'collection').desc(nulls_last=True), '-pk')


def sync_documents_for_tenant(tenant_id, user=None, transaction_ids=None, source_types=None,
                              modified_after=None, max_api_calls=None, offset=0,
                              calls_per_minute=DEFAULT_CALLS_PER_MINUTE,
                              only_flagged=True, headroom=None, force_ids=None):
    """
    FETCH pass: download attachments from Xero and save as XeroDocument linked to XeroTransactionSource.

    Args:
        tenant_id: Xero tenant ID
        user: Optional user (for credentials lookup)
        transaction_ids: Optional set or list of Xero transaction IDs (e.g. InvoiceID). When given,
            exactly those rows are visited regardless of has_attachments (explicit instruction / probe).
        source_types: Optional list of transaction_source values, e.g. ['Invoice', 'CreditNote']. If None, uses SUPPORTED_SOURCE_TYPES.
        modified_after: Optional aware datetime (incremental mode). Only flagged rows whose Xero
            UpdatedDateUTC is at/after this are visited (rows with no parseable UpdatedDateUTC are
            included as a safety net). Without it (backfill mode) every flagged row that has no
            stored document is visited, newest first.
        max_api_calls: Optional cap on Xero API calls this run; the sync stops cleanly when reached.
        offset: Skip the first N rows of the ordered queryset (legacy chunked-backfill resume point;
            with only_flagged=True the set shrinks as documents land, so offset is rarely needed).
        calls_per_minute: Throttle ceiling (Xero tenant limit is 60/min).
        only_flagged: Visit only rows with has_attachments=True (default). False = legacy probe-everything.
        headroom: Stop cleanly when X-DayLimit-Remaining (from Xero's response headers) drops below this.
        force_ids: Transaction IDs that bypass the modified_after check (typically the IDs the
            discovery pass just saw flagged — their stored UpdatedDateUTC may be stale).

    Returns:
        dict with: success, message, synced, errors, skipped, processed, api_calls,
        stopped_early (None | 'daily-limit' | 'max-api-calls' | 'headroom-floor'), next_offset,
        candidates (rows selected), remaining (flagged rows still without a document after this run),
        day_limit_remaining.
    """
    def _result(success, message, synced=0, errors=None, skipped=0, processed=0,
                api_calls=0, stopped_early=None, candidates=0, remaining=None,
                day_limit_remaining=None):
        return {
            'success': success, 'message': message, 'synced': synced,
            'errors': errors or [], 'skipped': skipped, 'processed': processed,
            'api_calls': api_calls, 'stopped_early': stopped_early,
            'next_offset': offset + processed, 'candidates': candidates,
            'remaining': remaining, 'day_limit_remaining': day_limit_remaining,
        }

    tenant, err = _load_tenant(tenant_id)
    if err:
        return _result(False, err)

    api_client, api = _build_api(tenant_id, user)

    types_to_sync = source_types or list(SUPPORTED_SOURCE_TYPES)
    qs = pending_fetch_queryset(tenant, types_to_sync, transaction_ids, only_flagged=only_flagged)
    if transaction_ids is None and only_flagged and modified_after is None:
        # Pure backfill mode: rows that already have a document are done.
        qs = qs.filter(has_doc=False)
    candidates = qs.count()
    if offset:
        qs = qs[offset:]

    call = _RateLimitedCaller(calls_per_minute=calls_per_minute, api_client=api_client, headroom=headroom)
    force_ids = set(force_ids or ())
    synced = 0
    errors = []
    skipped = 0
    processed = 0
    unparsed_dates = 0
    stopped_early = None

    for source in qs.iterator():
        if max_api_calls is not None and call.calls >= max_api_calls:
            stopped_early = 'max-api-calls'
            break

        txn_id = source.transactions_id
        txn_type = source.transaction_source
        getters = ATTACHMENT_GETTERS.get(txn_type)
        if not getters:
            processed += 1
            skipped += 1
            continue

        if modified_after is not None and txn_id not in force_ids:
            # Incremental mode: only rows Xero changed since the watermark (the
            # backlog of older flagged rows is the backfill's job, not the nightly's).
            # Rows discovery just saw flagged are forced in regardless — their
            # stored UpdatedDateUTC lags until the pipeline refreshes the record.
            updated = parse_xero_date((source.collection or {}).get('UpdatedDateUTC'))
            if updated is None:
                unparsed_dates += 1
            elif updated < modified_after:
                processed += 1
                skipped += 1
                continue

        list_fn, content_fn = getters
        try:
            serialized = call(list_fn, api, tenant_id, txn_id)
        except (DailyLimitReached, HeadroomFloorReached) as e:
            stopped_early = 'daily-limit' if isinstance(e, DailyLimitReached) else 'headroom-floor'
            errors.append(f"{txn_type} {txn_id}: {e}")
            break
        except Exception as e:
            processed += 1
            errors.append(f"{txn_type} {txn_id} list attachments: {e}")
            logger.debug("List attachments failed for %s %s: %s", txn_type, txn_id, e)
            continue

        attachments = _attachment_list_from_response(serialized)
        now = timezone.now()
        if not attachments:
            # Authoritative answer from the Attachments endpoint itself: nothing there.
            # Record it so the flagged-rows queue does not revisit this row every night.
            if source.has_attachments:
                logger.info("HasAttachments=true but Attachments list empty for %s %s; flag cleared",
                            txn_type, txn_id)
            XeroTransactionSource.objects.filter(pk=source.pk).update(
                has_attachments=False, attachments_checked_at=now)
            processed += 1
            continue
        XeroTransactionSource.objects.filter(pk=source.pk).update(
            has_attachments=True, attachments_checked_at=now)

        for att in attachments:
            att_id = att.get('AttachmentID') or att.get('attachment_id')
            file_name = att.get('FileName') or att.get('file_name') or 'attachment'
            if isinstance(file_name, dict):
                file_name = file_name.get('value', file_name.get('name', 'attachment')) or 'attachment'
            file_name = str(file_name)
            _mime = att.get('MimeType') or att.get('mime_type') or 'application/octet-stream'
            mime = (_mime if isinstance(_mime, str) else 'application/octet-stream').strip() or 'application/octet-stream'

            try:
                content = call(content_fn, api, tenant_id, txn_id, att_id, mime)
                data = _content_to_bytes(content)
            except (DailyLimitReached, HeadroomFloorReached) as e:
                stopped_early = 'daily-limit' if isinstance(e, DailyLimitReached) else 'headroom-floor'
                errors.append(f"{txn_type} {txn_id} attachment {file_name}: {e}")
                break
            except Exception as e:
                errors.append(f"{txn_type} {txn_id} attachment {file_name}: {e}")
                logger.debug("Get attachment content failed: %s", e)
                continue

            doc, created = XeroDocument.objects.update_or_create(
                organisation=tenant,
                transaction_source=source,
                file_name=file_name,
                defaults={
                    'content_type': mime,
                    'xero_attachment_id': str(att_id) if att_id else None,
                },
            )
            doc.file.save(file_name, ContentFile(data), save=True)
            synced += 1
            if created:
                logger.debug("Created document %s for %s %s", file_name, txn_type, txn_id)
            else:
                logger.debug("Updated document %s for %s %s", file_name, txn_type, txn_id)

        if stopped_early:
            # Limit hit mid-transaction: do not count it as processed so the
            # next run retries this transaction from scratch.
            break
        processed += 1

    if unparsed_dates:
        logger.warning(
            "%d transaction(s) had no parseable UpdatedDateUTC and were included in the incremental sync",
            unparsed_dates,
        )

    remaining = pending_fetch_queryset(tenant, types_to_sync, None, only_flagged=True).filter(has_doc=False).count()
    day_left = call.day_limit_remaining

    message = (f"Synced {synced} document(s) for tenant {tenant_id} ({call.calls} API calls, "
               f"{processed} of {candidates} candidate transactions processed; "
               f"{remaining} flagged transaction(s) still without documents)")
    if stopped_early:
        message += f"; stopped early: {stopped_early}, resume with offset={offset + processed}"
    if day_left is not None:
        message += f"; X-DayLimit-Remaining={day_left}"

    return _result(
        len(errors) == 0, message,
        synced=synced, errors=errors, skipped=skipped, processed=processed,
        api_calls=call.calls, stopped_early=stopped_early, candidates=candidates,
        remaining=remaining, day_limit_remaining=day_left,
    )


def validate_has_attachments_against_files_api(tenant_id, user=None, sample_size=200, source_types=None,
                                               chunk_size=40, calls_per_minute=DEFAULT_CALLS_PER_MINUTE):
    """
    One-off validation: compare HasAttachments (Accounting list field) with the Files API
    GET /Associations/Count?ObjectIds=... on a sample of known transaction IDs.

    Xero does not document that the two systems agree; this settles it for our data.
    Sample = half flagged-true, half flagged-false (most recent first). Requires the
    ``files.read`` (or ``files``) OAuth scope — without it the call returns 401/403
    and the result reports that instead of agreement figures.

    Returns dict: success, message, api_calls, sampled, agree, disagree,
        true_but_zero_assoc, false_but_assoc, scope_error, details (list of mismatches).
    """
    from xero_python.file import FilesApi

    result = {
        'success': False, 'message': '', 'api_calls': 0, 'sampled': 0, 'agree': 0,
        'disagree': 0, 'true_but_zero_assoc': 0, 'false_but_assoc': 0,
        'scope_error': None, 'details': [],
    }
    tenant, err = _load_tenant(tenant_id)
    if err:
        result['message'] = err
        return result

    types = source_types or list(SUPPORTED_SOURCE_TYPES)
    half = max(sample_size // 2, 1)
    base = XeroTransactionSource.objects.filter(organisation=tenant, transaction_source__in=types)
    order = KeyTextTransform('Date', 'collection').desc(nulls_last=True)
    pos = list(base.filter(has_attachments=True).order_by(order).values_list('transactions_id', flat=True)[:half])
    neg = list(base.filter(has_attachments=False).order_by(order).values_list('transactions_id', flat=True)[:half])
    expected = {tid: True for tid in pos}
    expected.update({tid: False for tid in neg})
    ids = list(expected)
    result['sampled'] = len(ids)
    if not ids:
        result['message'] = 'No rows with a known has_attachments flag to validate; run discovery first.'
        return result

    credentials = _get_credentials_for_tenant(tenant_id, user)
    api_client = XeroApiClient(credentials.user, tenant_id=tenant_id)
    files_api = FilesApi(api_client.api_client)
    call = _RateLimitedCaller(calls_per_minute=calls_per_minute, api_client=api_client)

    counts = {}
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        try:
            resp = call(files_api.get_associations_count, tenant_id, chunk)
        except DailyLimitReached:
            raise
        except Exception as e:
            status = getattr(e, 'status', None)
            if status in (401, 403):
                result['scope_error'] = (f"Files API returned {status}: the connected app lacks the "
                                         f"files.read scope; cannot validate Associations/Count ({e})")
                result['api_calls'] = call.calls
                result['message'] = result['scope_error']
                return result
            result['details'].append(f"chunk {i // chunk_size}: {e}")
            continue
        data = serialize_model(resp) if not isinstance(resp, dict) else resp
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    counts[str(k)] = int(v)
                except (TypeError, ValueError):
                    pass
    result['api_calls'] = call.calls

    for tid, flag in expected.items():
        n = counts.get(tid)
        if n is None:
            result['details'].append(f"{tid}: no count returned (expected HasAttachments={flag})")
            continue
        if (n > 0) == flag:
            result['agree'] += 1
        else:
            result['disagree'] += 1
            if flag and n == 0:
                result['true_but_zero_assoc'] += 1
            elif not flag and n > 0:
                result['false_but_assoc'] += 1
            result['details'].append(f"{tid}: HasAttachments={flag} but Associations/Count={n}")
    result['success'] = result['disagree'] == 0 and not result['details']
    result['message'] = (f"Validated {result['sampled']} IDs against Files API Associations/Count in "
                         f"{call.calls} call(s): {result['agree']} agree, {result['disagree']} disagree "
                         f"(HasAttachments=true but 0 associations: {result['true_but_zero_assoc']}; "
                         f"HasAttachments=false but >0 associations: {result['false_but_assoc']})")
    return result
