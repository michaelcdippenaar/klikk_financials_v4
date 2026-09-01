"""
The ONE write path to Xero: create a DRAFT invoice, audit-logged.

House rules this module enforces (see the repo CLAUDE.md "Xero — HARD RULE"):

- **DRAFT only.** The created invoice sits in Xero's Draft queue until a human
  approves it there; this module cannot authorise, pay, or modify anything.
- **Logged BEFORE it happens.** A row goes into ``audit.xero_writes`` before the
  API call fires, and is updated with Xero's response after. If the process
  dies mid-write, the pending row is the evidence.
- **Explicit instruction.** Callers must pass ``instruction`` — MC's words that
  authorised this specific write. It is stored verbatim in the log.
- **Smallest write.** One invoice per call. No batch endpoint on purpose.
- **No side-creation.** Contacts must already exist (resolved against the local
  mirror); unknown contacts are a 400 with candidates, never an implicit create.

Rate-limit posture (xero-api-integration skill): this is a single POST per call
through XeroApiClient, which carries the HTTP guards (timeouts, daily-limit
conversion, quota telemetry) and the single-flight token refresh. A
DailyLimitReached or TenantReauthRequired surfaces as a clean error, never a
retry loop.
"""
import datetime as dt
import json
import logging
from decimal import Decimal, InvalidOperation

from django.db import connection

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_core.services import (
    DailyLimitReached, TenantReauthRequired, XeroApiClient,
)
from apps.xero.xero_data.services import _get_credentials_for_tenant
from apps.xero.xero_metadata.models import XeroAccount, XeroContacts

logger = logging.getLogger(__name__)

MAX_LINE_ITEMS = 50
VALID_TYPES = {'ACCREC', 'ACCPAY'}
VALID_LINE_AMOUNT_TYPES = {'Exclusive', 'Inclusive', 'NoTax'}


class InvoiceValidationError(Exception):
    """Carries a list of human-readable problems plus optional extras (e.g. candidates)."""

    def __init__(self, problems, extra=None):
        super().__init__('; '.join(problems))
        self.problems = problems
        self.extra = extra or {}


def _parse_date(value, field):
    if value in (None, ''):
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise InvoiceValidationError([f'{field} must be YYYY-MM-DD, got {value!r}'])


def _parse_decimal(value, field, problems):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        problems.append(f'{field} must be a number, got {value!r}')
        return None
    return d


def resolve_contact(tenant, contact_id, contact_name):
    """Resolve to a mirrored contact. Ambiguity or absence is a 400 with candidates —
    creating contacts implicitly would be a second, unlogged write."""
    qs = XeroContacts.objects.filter(organisation=tenant)
    if contact_id:
        contact = qs.filter(contacts_id=str(contact_id).strip()).first()
        if not contact:
            raise InvoiceValidationError(
                [f'contact_id {contact_id!r} is not in the local mirror for this tenant '
                 '(sync contacts first, or check the id)'])
        return contact
    name = str(contact_name or '').strip()
    if not name:
        raise InvoiceValidationError(['contact_id or contact_name is required'])
    exact = list(qs.filter(name__iexact=name)[:2])
    if len(exact) == 1:
        return exact[0]
    candidates = list(qs.filter(name__icontains=name).values('contacts_id', 'name')[:8])
    if len(exact) > 1:
        raise InvoiceValidationError(
            [f'contact name {name!r} matches more than one contact — pass contact_id'],
            extra={'candidates': candidates})
    raise InvoiceValidationError(
        [f'no contact named {name!r} in the local mirror for this tenant — '
         'pick from candidates (pass contact_id) or ask MC; contacts are never auto-created'],
        extra={'candidates': candidates})


def validate_payload(payload):
    """Validate + normalise. Returns (tenant, contact, normalised_dict)."""
    problems = []

    if payload.get('confirm') is not True:
        problems.append('confirm must be literally true — this call writes a DRAFT invoice to Xero')
    instruction = str(payload.get('instruction') or '').strip()
    if not instruction:
        problems.append("instruction is required — MC's words authorising this specific write, quoted verbatim")

    tenant_id = str(payload.get('tenant_id') or '').strip()
    tenant = XeroTenant.objects.filter(tenant_id=tenant_id).first() if tenant_id else None
    if not tenant:
        problems.append(f'tenant_id {tenant_id!r} is not a known Xero tenant')

    inv_type = str(payload.get('type') or 'ACCREC').strip().upper()
    if inv_type not in VALID_TYPES:
        problems.append(f'type must be one of {sorted(VALID_TYPES)}, got {inv_type!r}')

    line_amount_types = str(payload.get('line_amount_types') or 'Exclusive').strip()
    if line_amount_types not in VALID_LINE_AMOUNT_TYPES:
        problems.append(f'line_amount_types must be one of {sorted(VALID_LINE_AMOUNT_TYPES)}')

    raw_lines = payload.get('line_items')
    if not isinstance(raw_lines, list) or not raw_lines:
        problems.append('line_items must be a non-empty array')
        raw_lines = []
    if len(raw_lines) > MAX_LINE_ITEMS:
        problems.append(f'at most {MAX_LINE_ITEMS} line items per invoice')
        raw_lines = []

    known_codes = set(
        XeroAccount.objects.filter(organisation=tenant).values_list('code', flat=True)
    ) if tenant else set()

    lines = []
    for i, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            problems.append(f'line_items[{i}] must be an object')
            continue
        desc = str(raw.get('description') or '').strip()
        if not desc:
            problems.append(f'line_items[{i}].description is required')
        qty = _parse_decimal(raw.get('quantity', 1), f'line_items[{i}].quantity', problems)
        unit = _parse_decimal(raw.get('unit_amount'), f'line_items[{i}].unit_amount', problems)
        code = str(raw.get('account_code') or '').strip()
        if not code:
            problems.append(f'line_items[{i}].account_code is required')
        elif known_codes and code not in known_codes:
            problems.append(
                f'line_items[{i}].account_code {code!r} is not in the chart of accounts '
                'for this tenant (kb_lookup_account can find the right code)')
        line = {
            'description': desc,
            'quantity': qty,
            'unit_amount': unit,
            'account_code': code,
        }
        if raw.get('tax_type'):
            line['tax_type'] = str(raw['tax_type']).strip()
        tracking = raw.get('tracking')
        if tracking:
            if not isinstance(tracking, list):
                problems.append(f'line_items[{i}].tracking must be an array of {{name, option}}')
            else:
                line['tracking'] = [
                    {'name': str(t.get('name') or '').strip(), 'option': str(t.get('option') or '').strip()}
                    for t in tracking if isinstance(t, dict)
                ]
                if any(not t['name'] or not t['option'] for t in line['tracking']):
                    problems.append(f'line_items[{i}].tracking entries need both name and option')
        lines.append(line)

    date = _parse_date(payload.get('date'), 'date') or dt.date.today()
    due_date = _parse_date(payload.get('due_date'), 'due_date')

    if problems:
        raise InvoiceValidationError(problems)

    contact = resolve_contact(tenant, payload.get('contact_id'), payload.get('contact_name'))

    return tenant, contact, {
        'type': inv_type,
        'date': date,
        'due_date': due_date,
        'reference': str(payload.get('reference') or '').strip(),
        'currency_code': str(payload.get('currency_code') or 'ZAR').strip().upper(),
        'line_amount_types': line_amount_types,
        'line_items': lines,
        'instruction': instruction,
        'instructed_by': str(payload.get('instructed_by') or 'MC').strip() or 'MC',
    }


def _log_write(tenant, operation, object_type, object_id, object_ref, payload,
               reversal_hint, instructed_by, instruction_quote):
    with connection.cursor() as cur:
        cur.execute(
            '''insert into audit.xero_writes
               (tenant_id, tenant_name, operation, object_type, object_id, object_ref,
                payload, reversal_hint, instructed_by, instruction_quote, performed_by)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               returning id''',
            [tenant.tenant_id, tenant.tenant_name, operation, object_type, object_id,
             object_ref, json.dumps(payload, default=str), reversal_hint,
             instructed_by, instruction_quote, 'claude'],
        )
        return cur.fetchone()[0]


def _update_log(log_id, *, object_id=None, object_ref=None, xero_response=None,
                http_status=None, reversal_hint=None):
    sets, args = [], []
    if object_id is not None:
        sets.append('object_id = %s'); args.append(object_id)
    if object_ref is not None:
        sets.append('object_ref = %s'); args.append(object_ref)
    if xero_response is not None:
        sets.append('xero_response = %s'); args.append(json.dumps(xero_response, default=str))
    if http_status is not None:
        sets.append('http_status = %s'); args.append(http_status)
    if reversal_hint is not None:
        sets.append('reversal_hint = %s'); args.append(reversal_hint)
    if not sets:
        return
    with connection.cursor() as cur:
        cur.execute(f'update audit.xero_writes set {", ".join(sets)} where id = %s', [*args, log_id])


def create_draft_invoice(payload):
    """Validate, log, then create ONE draft invoice in Xero. Returns a summary dict.

    Raises InvoiceValidationError (caller maps to 400), TenantReauthRequired,
    DailyLimitReached, or lets an SDK ApiException propagate after the audit row
    is updated with the failure.
    """
    tenant, contact, norm = validate_payload(payload)

    # Log FIRST. If anything fails after this, the pending row (http_status null)
    # is the record that a write was attempted.
    log_id = _log_write(
        tenant=tenant,
        operation='create_invoice_draft',
        object_type='Invoice',
        object_id='(pending)',
        object_ref=norm['reference'] or None,
        payload={**norm, 'contact_id': contact.contacts_id, 'contact_name': contact.name},
        reversal_hint='Write did not complete (or crashed mid-flight): check Xero Drafts '
                      f'for tenant {tenant.tenant_name} before assuming nothing was created.',
        instructed_by=norm['instructed_by'],
        instruction_quote=norm['instruction'],
    )

    from xero_python.accounting import (
        Contact, Invoice, Invoices, LineItem, LineItemTracking,
    )

    line_items = []
    for line in norm['line_items']:
        kwargs = {
            'description': line['description'],
            'quantity': float(line['quantity']),
            'unit_amount': float(line['unit_amount']),
            'account_code': line['account_code'],
        }
        if line.get('tax_type'):
            kwargs['tax_type'] = line['tax_type']
        if line.get('tracking'):
            kwargs['tracking'] = [
                LineItemTracking(name=t['name'], option=t['option']) for t in line['tracking']
            ]
        line_items.append(LineItem(**kwargs))

    invoice = Invoice(
        type=norm['type'],
        contact=Contact(contact_id=contact.contacts_id),
        date=norm['date'],
        due_date=norm['due_date'],
        line_items=line_items,
        reference=norm['reference'] or None,
        currency_code=norm['currency_code'],
        line_amount_types=norm['line_amount_types'],
        status='DRAFT',
    )

    credentials = _get_credentials_for_tenant(tenant.tenant_id)
    client = XeroApiClient(credentials.user, tenant.tenant_id)
    from xero_python.accounting import AccountingApi
    api = AccountingApi(client.api_client)

    try:
        result = api.create_invoices(
            tenant.tenant_id, invoices=Invoices(invoices=[invoice]), summarize_errors=False,
        )
    except Exception as exc:
        status = getattr(exc, 'status', None)
        body = getattr(exc, 'body', None)
        try:
            body = json.loads(body) if isinstance(body, (str, bytes)) else body
        except (ValueError, TypeError):
            pass
        _update_log(log_id, xero_response={'error': str(exc), 'body': body},
                    http_status=status if isinstance(status, int) else None,
                    reversal_hint='Xero rejected the write — nothing to reverse. '
                                  'Verify no draft appeared before retrying.')
        logger.warning('Draft invoice write failed (log id %s): %s', log_id, exc)
        raise

    created = result.invoices[0]
    validation_errors = [e.message for e in (created.validation_errors or [])]
    if validation_errors:
        _update_log(log_id, xero_response={'validation_errors': validation_errors},
                    http_status=400,
                    reversal_hint='Xero rejected the invoice with validation errors — nothing created.')
        raise InvoiceValidationError([f'Xero rejected the invoice: {"; ".join(validation_errors)}'])

    invoice_id = str(created.invoice_id)
    invoice_number = created.invoice_number or ''
    _update_log(
        log_id,
        object_id=invoice_id,
        object_ref=invoice_number or norm['reference'] or None,
        xero_response={
            'invoice_id': invoice_id,
            'invoice_number': invoice_number,
            'status': str(created.status),
            'total': str(created.total),
            'sub_total': str(created.sub_total),
            'total_tax': str(created.total_tax),
        },
        http_status=200,
        reversal_hint=f'DRAFT invoice {invoice_number or invoice_id} in {tenant.tenant_name}: '
                      'delete it from the Drafts queue in Xero (or POST status DELETED). '
                      'It affects no ledger until a human approves it.',
    )

    logger.info('Created DRAFT invoice %s (%s) in %s — audit.xero_writes id %s',
                invoice_number or invoice_id, norm['type'], tenant.tenant_name, log_id)

    return {
        'write_log_id': log_id,
        'tenant_id': tenant.tenant_id,
        'tenant_name': tenant.tenant_name,
        'invoice_id': invoice_id,
        'invoice_number': invoice_number,
        'status': str(created.status),
        'type': norm['type'],
        'contact': {'contact_id': contact.contacts_id, 'name': contact.name},
        'date': str(norm['date']),
        'due_date': str(norm['due_date']) if norm['due_date'] else None,
        'reference': norm['reference'] or None,
        'currency_code': norm['currency_code'],
        'line_amount_types': norm['line_amount_types'],
        'sub_total': str(created.sub_total),
        'total_tax': str(created.total_tax),
        'total': str(created.total),
        'line_count': len(norm['line_items']),
    }
