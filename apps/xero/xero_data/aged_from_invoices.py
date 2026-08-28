"""Aged payables and receivables computed from invoices we already hold.

Xero has no bulk aged-report endpoint: AgedPayablesByContact and
AgedReceivablesByContact each take a single contact, so the previous
implementation issued one API call per contact and walked the entire contact
list. On the Klikk tenant that is hundreds of calls against a 1,000/day
allowance, and on 28 Aug 2026 one run spent roughly 83% of the day's budget.

The deeper problem was not pacing. It was that the sweep called every contact
to discover that almost none of them owe anything: Klikk has 46 outstanding
invoices across 25 contacts. An aged report is a bucketing of unpaid invoice
balances by how overdue they are, and we already hold every invoice, including
``amount_due`` — whose own help text says it "drives aged-AR/AP queries".

So this computes the same answer from local data, in one query, with **no Xero
API calls at all**.

BUCKET BOUNDARIES ARE NOT YET CONFIRMED AGAINST XERO. No AgedPayable or
AgedReceivable row has ever been written for any tenant, so there was nothing to
calibrate against offline. The bands below are the standard reading of Xero's
column names, isolated in ``bucket_for`` and covered by tests, and
``manage.py verify_aged_against_xero`` checks them against Xero for only the
contacts that actually carry a balance — 25 calls on Klikk, not hundreds.
Until that has been run, treat the bands as declared rather than verified.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.xero.xero_data.models import (
    AgedPayable,
    AgedReceivable,
    XeroInvoice,
    XeroInvoiceStatus,
    XeroInvoiceType,
)

logger = logging.getLogger(__name__)

# A balance is outstanding only if the invoice is live and unpaid. Drafts are
# excluded: Xero does not age an invoice the customer has never been sent.
OUTSTANDING_STATUSES = (XeroInvoiceStatus.AUTHORISED, XeroInvoiceStatus.SUBMITTED)

CURRENT = 'current'
ONE_MONTH = 'one_month'
TWO_MONTHS = 'two_months'
THREE_MONTHS = 'three_months'
OLDER = 'older'
BUCKETS = (CURRENT, ONE_MONTH, TWO_MONTHS, THREE_MONTHS, OLDER)


def months_overdue(due_date, report_date):
    """Whole calendar months by which ``due_date`` precedes ``report_date``.

    Calendar months, not 30-day blocks: Xero's columns are months, and a
    31-day February would otherwise drift a bucket.
    """
    if due_date is None or due_date >= report_date:
        return 0
    months = (report_date.year - due_date.year) * 12 + (report_date.month - due_date.month)
    if report_date.day < due_date.day:
        months -= 1
    return max(months, 0)


def bucket_for(due_date, report_date):
    """Which aged column a balance falls in.

    Current is not-yet-due. Each later column is one further whole month
    overdue, and anything beyond three months is Older.
    """
    if due_date is None or due_date >= report_date:
        return CURRENT
    months = months_overdue(due_date, report_date)
    if months < 1:
        return ONE_MONTH
    if months < 2:
        return TWO_MONTHS
    if months < 3:
        return THREE_MONTHS
    return OLDER


def build_aged(tenant, invoice_type, report_date):
    """Per-contact aged buckets from local invoices. One query, no API calls."""
    invoices = (
        XeroInvoice.objects
        .filter(
            organisation=tenant,
            type=invoice_type,
            status__in=OUTSTANDING_STATUSES,
            date__lte=report_date,
        )
        .exclude(amount_due=0)
        .values('xero_contact_id', 'contact_name', 'due_date', 'amount_due')
    )

    by_contact = {}
    for invoice in invoices:
        contact_id = invoice['xero_contact_id']
        if not contact_id:
            # Without a contact id the row cannot be attributed, and an
            # unattributed aged balance is worse than an absent one.
            continue
        entry = by_contact.setdefault(contact_id, {
            'contact_name': invoice['contact_name'] or '',
            **{bucket: Decimal('0') for bucket in BUCKETS},
            'total': Decimal('0'),
        })
        amount = Decimal(invoice['amount_due'] or 0)
        entry[bucket_for(invoice['due_date'], report_date)] += amount
        entry['total'] += amount
    return by_contact


def _sync(tenant, *, invoice_type, model, report_date=None):
    report_date = report_date or timezone.localdate()
    by_contact = build_aged(tenant, invoice_type, report_date)

    stats = {
        'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0,
        'contact_count': len(by_contact), 'contacts_processed': len(by_contact),
        'api_calls': 0, 'stopped_early': None,
    }

    with transaction.atomic():
        # A contact that has settled up must lose its stale row, or the screen
        # keeps reporting a balance that no longer exists.
        model.objects.filter(tenant=tenant, report_date=report_date).exclude(
            contact_id__in=list(by_contact)
        ).delete()

        for contact_id, entry in by_contact.items():
            if entry['total'] == 0:
                stats['skipped'] += 1
                continue
            _, created = model.objects.update_or_create(
                tenant=tenant,
                contact_id=contact_id,
                report_date=report_date,
                defaults={
                    'contact_name': entry['contact_name'],
                    **{bucket: entry[bucket] for bucket in BUCKETS},
                    'total': entry['total'],
                },
            )
            stats['created' if created else 'updated'] += 1

    stats['completed_at'] = timezone.now().isoformat()
    logger.info(
        'Aged %s built from local invoices for tenant %s: %s',
        model.__name__, tenant.tenant_id, stats,
    )
    return stats


def sync_aged_payables_from_invoices(tenant, report_date=None, **_ignored):
    return _sync(tenant, invoice_type=XeroInvoiceType.ACCPAY,
                 model=AgedPayable, report_date=report_date)


def sync_aged_receivables_from_invoices(tenant, report_date=None, **_ignored):
    return _sync(tenant, invoice_type=XeroInvoiceType.ACCREC,
                 model=AgedReceivable, report_date=report_date)
