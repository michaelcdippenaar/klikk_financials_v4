"""
Import documents (attachments) from Xero and link them to transactions in the DB.

Requires Xero OAuth scope: accounting.attachments or accounting.attachments.read.

Supported transaction types: Invoice, CreditNote, BankTransaction.
"""
import logging
import os
import re
import time
from datetime import datetime, timezone as dt_timezone

from django.core.files.base import ContentFile

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

# Transaction types that support attachments in Xero Accounting API
SUPPORTED_SOURCE_TYPES = ('Invoice', 'CreditNote', 'BankTransaction')

# Xero tenant limits: 60 calls/min, 5000 calls/day.
DEFAULT_CALLS_PER_MINUTE = 55

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


class _RateLimitedCaller:
    """Throttles Xero API calls under the per-minute limit; waits out minute-window 429s,
    raises DailyLimitReached when the daily limit is hit."""

    def __init__(self, calls_per_minute=DEFAULT_CALLS_PER_MINUTE):
        self.min_interval = 60.0 / calls_per_minute
        self.calls = 0
        self._last_call = 0.0

    def __call__(self, fn, *args):
        for attempt in (1, 2):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                return fn(*args)
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


def sync_documents_for_tenant(tenant_id, user=None, transaction_ids=None, source_types=None,
                              modified_after=None, max_api_calls=None, offset=0,
                              calls_per_minute=DEFAULT_CALLS_PER_MINUTE):
    """
    Fetch attachments from Xero for transactions and save as XeroDocument linked to XeroTransactionSource.

    Args:
        tenant_id: Xero tenant ID
        user: Optional user (for credentials lookup)
        transaction_ids: Optional set or list of Xero transaction IDs (e.g. InvoiceID). If None, syncs for all supported types.
        source_types: Optional list of transaction_source values, e.g. ['Invoice', 'CreditNote']. If None, uses SUPPORTED_SOURCE_TYPES.
        modified_after: Optional aware datetime; only transactions whose Xero UpdatedDateUTC is at or
            after this are synced (transactions with no parseable UpdatedDateUTC are included as a safety net).
        max_api_calls: Optional cap on Xero API calls this run; the sync stops cleanly when reached.
        offset: Skip the first N transactions of the (pk-ordered) queryset — resume point for chunked backfills.
        calls_per_minute: Throttle ceiling (Xero tenant limit is 60/min).

    Returns:
        dict with: success, message, synced, errors, skipped, processed, api_calls,
        stopped_early (None | 'daily-limit' | 'max-api-calls'), next_offset.
    """
    def _result(success, message, synced=0, errors=None, skipped=0, processed=0,
                api_calls=0, stopped_early=None):
        return {
            'success': success, 'message': message, 'synced': synced,
            'errors': errors or [], 'skipped': skipped, 'processed': processed,
            'api_calls': api_calls, 'stopped_early': stopped_early,
            'next_offset': offset + processed,
        }

    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return _result(False, f'Tenant {tenant_id} not found')

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
    api = xero_api.api_client

    types_to_sync = source_types or list(SUPPORTED_SOURCE_TYPES)
    qs = XeroTransactionSource.objects.filter(
        organisation=tenant,
        transaction_source__in=types_to_sync,
    )
    if transaction_ids is not None:
        qs = qs.filter(transactions_id__in=transaction_ids)
    qs = qs.order_by('pk')
    if offset:
        qs = qs[offset:]

    call = _RateLimitedCaller(calls_per_minute=calls_per_minute)
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

        if modified_after is not None:
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
        except XeroDailyLimitReached as e:
            stopped_early = 'daily-limit'
            errors.append(f"{txn_type} {txn_id}: {e}")
            break
        except Exception as e:
            processed += 1
            errors.append(f"{txn_type} {txn_id} list attachments: {e}")
            logger.debug("List attachments failed for %s %s: %s", txn_type, txn_id, e)
            continue

        attachments = _attachment_list_from_response(serialized)
        if not attachments:
            processed += 1
            continue

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
            except XeroDailyLimitReached as e:
                stopped_early = 'daily-limit'
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
            # Daily limit hit mid-transaction: do not count it as processed so the
            # next run's offset retries this transaction from scratch.
            break
        processed += 1

    if unparsed_dates:
        logger.warning(
            "%d transaction(s) had no parseable UpdatedDateUTC and were included in the incremental sync",
            unparsed_dates,
        )

    message = f"Synced {synced} document(s) for tenant {tenant_id} ({call.calls} API calls, {processed} transactions processed)"
    if stopped_early:
        message += f"; stopped early: {stopped_early}, resume with offset={offset + processed}"

    result = _result(
        len(errors) == 0, message,
        synced=synced, errors=errors, skipped=skipped, processed=processed,
        api_calls=call.calls, stopped_early=stopped_early,
    )
    return result
