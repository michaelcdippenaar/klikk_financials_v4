"""Aged payables and receivables derived from invoices we already hold.

Xero has no bulk aged endpoint, so the previous implementation issued one API
call per contact and walked the whole contact list — hundreds of calls against
a 1,000/day allowance to discover that, on Klikk, 46 invoices across 25
contacts carry a balance. These tests pin the replacement: the same answer, no
API calls, and honest about what it excludes.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.aged_from_invoices import (
    bucket_for,
    build_aged,
    months_overdue,
    sync_aged_payables_from_invoices,
    sync_aged_receivables_from_invoices,
)
from apps.xero.xero_data.models import (
    AgedPayable,
    AgedReceivable,
    XeroInvoice,
    XeroInvoiceStatus,
    XeroInvoiceType,
)

REPORT_DATE = date(2026, 8, 31)


class AgedBucketTests(TestCase):
    def test_a_balance_not_yet_due_is_current(self):
        self.assertEqual(bucket_for(date(2026, 9, 15), REPORT_DATE), 'current')
        self.assertEqual(bucket_for(REPORT_DATE, REPORT_DATE), 'current')

    def test_each_further_month_overdue_moves_one_column(self):
        self.assertEqual(bucket_for(date(2026, 8, 20), REPORT_DATE), 'one_month')
        self.assertEqual(bucket_for(date(2026, 7, 20), REPORT_DATE), 'two_months')
        self.assertEqual(bucket_for(date(2026, 6, 20), REPORT_DATE), 'three_months')
        self.assertEqual(bucket_for(date(2026, 5, 20), REPORT_DATE), 'older')

    def test_months_are_calendar_months_not_thirty_day_blocks(self):
        # A 28-day February must not drift a balance into the wrong column.
        self.assertEqual(months_overdue(date(2026, 1, 31), date(2026, 2, 28)), 0)
        self.assertEqual(months_overdue(date(2026, 1, 15), date(2026, 2, 28)), 1)

    def test_a_missing_due_date_is_treated_as_current_not_dropped(self):
        self.assertEqual(bucket_for(None, REPORT_DATE), 'current')


class AgedFromInvoicesTests(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-aged-inv', tenant_name='Aged Inv Co',
        )

    def _invoice(self, **kwargs):
        defaults = {
            'organisation': self.tenant,
            'invoice_id': f'inv-{XeroInvoice.objects.count() + 1}',
            'type': XeroInvoiceType.ACCPAY,
            'status': XeroInvoiceStatus.AUTHORISED,
            'xero_contact_id': 'contact-1',
            'contact_name': 'Supplier One',
            'date': date(2026, 6, 1),
            'due_date': date(2026, 8, 20),
            'amount_due': Decimal('100'),
        }
        defaults.update(kwargs)
        return XeroInvoice.objects.create(**defaults)

    def test_balances_are_bucketed_and_totalled_per_contact(self):
        self._invoice(due_date=date(2026, 9, 30), amount_due=Decimal('50'))    # current
        self._invoice(due_date=date(2026, 8, 20), amount_due=Decimal('70'))    # 1 month
        self._invoice(due_date=date(2026, 5, 1), amount_due=Decimal('30'))     # older

        aged = build_aged(self.tenant, XeroInvoiceType.ACCPAY, REPORT_DATE)['contact-1']

        self.assertEqual(aged['current'], Decimal('50'))
        self.assertEqual(aged['one_month'], Decimal('70'))
        self.assertEqual(aged['older'], Decimal('30'))
        self.assertEqual(aged['total'], Decimal('150'))

    def test_paid_void_and_draft_invoices_carry_no_aged_balance(self):
        self._invoice(status=XeroInvoiceStatus.PAID, amount_due=Decimal('0'))
        self._invoice(status=XeroInvoiceStatus.VOIDED, amount_due=Decimal('999'))
        self._invoice(status=XeroInvoiceStatus.DRAFT, amount_due=Decimal('999'))

        self.assertEqual(build_aged(self.tenant, XeroInvoiceType.ACCPAY, REPORT_DATE), {})

    def test_payables_and_receivables_do_not_bleed_into_each_other(self):
        self._invoice(type=XeroInvoiceType.ACCPAY, amount_due=Decimal('100'))
        self._invoice(type=XeroInvoiceType.ACCREC, amount_due=Decimal('250'),
                      xero_contact_id='contact-2', contact_name='Customer Two')

        sync_aged_payables_from_invoices(self.tenant, report_date=REPORT_DATE)
        sync_aged_receivables_from_invoices(self.tenant, report_date=REPORT_DATE)

        self.assertEqual([(r.contact_id, r.total) for r in AgedPayable.objects.all()],
                         [('contact-1', Decimal('100.00'))])
        self.assertEqual([(r.contact_id, r.total) for r in AgedReceivable.objects.all()],
                         [('contact-2', Decimal('250.00'))])

    def test_an_invoice_raised_after_the_report_date_is_not_aged_into_it(self):
        self._invoice(date=date(2026, 12, 1), due_date=date(2026, 12, 31))

        self.assertEqual(build_aged(self.tenant, XeroInvoiceType.ACCPAY, REPORT_DATE), {})

    def test_a_contact_that_settles_up_loses_its_stale_row(self):
        self._invoice(amount_due=Decimal('100'))
        sync_aged_payables_from_invoices(self.tenant, report_date=REPORT_DATE)
        self.assertEqual(AgedPayable.objects.count(), 1)

        XeroInvoice.objects.update(status=XeroInvoiceStatus.PAID, amount_due=Decimal('0'))
        sync_aged_payables_from_invoices(self.tenant, report_date=REPORT_DATE)

        # Leaving the row would keep reporting a debt that no longer exists.
        self.assertEqual(AgedPayable.objects.count(), 0)

    def test_an_unattributable_balance_is_skipped_rather_than_mis_assigned(self):
        self._invoice(xero_contact_id='', amount_due=Decimal('500'))

        self.assertEqual(build_aged(self.tenant, XeroInvoiceType.ACCPAY, REPORT_DATE), {})

    def test_the_sweep_reports_that_it_spent_no_api_calls(self):
        self._invoice()

        stats = sync_aged_payables_from_invoices(self.tenant, report_date=REPORT_DATE)

        self.assertEqual(stats['api_calls'], 0)
        self.assertEqual(stats['errors'], 0)
        self.assertIsNone(stats['stopped_early'])
        self.assertEqual(stats['created'], 1)
