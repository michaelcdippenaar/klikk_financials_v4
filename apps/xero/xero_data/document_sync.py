"""
Import documents (attachments) from Xero and link them to transactions in the DB.

Requires Xero OAuth scope: accounting.attachments or accounting.attachments.read.

Supported transaction types: Invoice, CreditNote, BankTransaction.
"""
import logging
from django.core.files.base import ContentFile

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import XeroTransactionSource, XeroDocument
from apps.xero.xero_data.services import _get_credentials_for_tenant
from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi, serialize_model

logger = logging.getLogger(__name__)

# Transaction types that support attachments in Xero Accounting API
SUPPORTED_SOURCE_TYPES = ('Invoice', 'CreditNote', 'BankTransaction')

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


def sync_documents_for_tenant(tenant_id, user=None, transaction_ids=None, source_types=None):
    """
    Fetch attachments from Xero for transactions and save as XeroDocument linked to XeroTransactionSource.

    Args:
        tenant_id: Xero tenant ID
        user: Optional user (for credentials lookup)
        transaction_ids: Optional set or list of Xero transaction IDs (e.g. InvoiceID). If None, syncs for all supported types.
        source_types: Optional list of transaction_source values, e.g. ['Invoice', 'CreditNote']. If None, uses SUPPORTED_SOURCE_TYPES.

    Returns:
        dict: { 'success': bool, 'message': str, 'synced': int, 'errors': list, 'skipped': int }
    """
    try:
        tenant = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return {'success': False, 'message': f'Tenant {tenant_id} not found', 'synced': 0, 'errors': [], 'skipped': 0}

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

    synced = 0
    errors = []
    skipped = 0

    for source in qs:
        txn_id = source.transactions_id
        txn_type = source.transaction_source
        getters = ATTACHMENT_GETTERS.get(txn_type)
        if not getters:
            skipped += 1
            continue

        list_fn, content_fn = getters
        try:
            serialized = list_fn(api, tenant_id, txn_id)
        except Exception as e:
            errors.append(f"{txn_type} {txn_id} list attachments: {e}")
            logger.debug("List attachments failed for %s %s: %s", txn_type, txn_id, e)
            continue

        attachments = _attachment_list_from_response(serialized)
        if not attachments:
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
                content = content_fn(api, tenant_id, txn_id, att_id, mime)
                data = _content_to_bytes(content)
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

    return {
        'success': len(errors) == 0,
        'message': f"Synced {synced} document(s) for tenant {tenant_id}",
        'synced': synced,
        'errors': errors,
        'skipped': skipped,
    }
