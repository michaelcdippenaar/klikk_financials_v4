"""Trial-balance reconciliation.

The comparison basis is the whole design. V1 compared every account against a
single calendar month, which is right for neither class of account: measured on
Klikk's real 2026-08-31 report that scored 3 matches out of 128, with R313m of
reported variance, and the screen built on it was never trusted.

Profit-and-loss accounts reset each fiscal year and must meet a fiscal-year-to-
date sum. Balance-sheet accounts carry forward and must meet an inception-to-
date sum. These tests pin that, and pin that an account we cannot classify is
declared rather than guessed.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.web_api_v2.services.xero_reconciliation import (
    BASIS_FISCAL_YTD,
    BASIS_INCEPTION,
    STATUS_MATCHED,
    STATUS_MISSING_IN_LEDGER,
    STATUS_MISSING_IN_XERO,
    STATUS_UNCLASSIFIED,
    STATUS_VARIANCE,
    build_reconciliation,
)
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_cube.models import XeroTrailBalance
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_validation.models import (
    XeroTrailBalanceReport,
    XeroTrailBalanceReportLine,
)


class ReconciliationBasisTests(TestCase):
    def setUp(self):
        # July fiscal start, as Klikk. The report is August, so the fiscal year
        # to date is July + August while inception to date reaches back years.
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-recon', tenant_name='Recon Co', fiscal_year_start_month=7,
        )
        self.report = XeroTrailBalanceReport.objects.create(
            organisation=self.tenant, report_date=date(2026, 8, 31),
        )

    def _account(self, code, name, klass, type_='EXPENSE'):
        return XeroAccount.objects.create(
            organisation=self.tenant, account_id=f'acc-{code}', code=code,
            name=name, type=type_, collection={'Class': klass} if klass else {},
        )

    def _line(self, account, value, code=None, name=None):
        return XeroTrailBalanceReportLine.objects.create(
            report=self.report, account=account,
            account_code=code or (account.code if account else ''),
            account_name=name or (account.name if account else ''),
            value=Decimal(value), row_type='Row',
        )

    def _ledger(self, account, year, month, amount):
        # fin_year/fin_period are the fiscal labels the consolidation writes;
        # the reconciliation reads calendar year/month, but they are NOT NULL.
        fiscal_year = year + 1 if month >= 7 else year
        fiscal_period = month - 6 if month >= 7 else month + 6
        XeroTrailBalance.objects.create(
            organisation=self.tenant, account=account, date=date(year, month, 1),
            year=year, month=month, fin_year=fiscal_year, fin_period=fiscal_period,
            amount=Decimal(amount), debit=Decimal(amount), credit=Decimal('0'),
        )

    def _row(self, result, code):
        return next(row for row in result['rows'] if row['accountCode'] == code)

    def test_a_profit_and_loss_account_meets_the_fiscal_year_to_date(self):
        account = self._account('4000', 'Revenue', 'REVENUE', 'REVENUE')
        self._ledger(account, 2026, 5, '900')    # prior fiscal year — excluded
        self._ledger(account, 2026, 7, '400')    # this fiscal year
        self._ledger(account, 2026, 8, '600')
        self._line(account, '1000')

        row = self._row(build_reconciliation(self.tenant), '4000')

        self.assertEqual(row['basis'], BASIS_FISCAL_YTD)
        self.assertEqual(row['ledgerValue'], Decimal('1000'))
        self.assertEqual(row['status'], STATUS_MATCHED)

    def test_a_balance_sheet_account_meets_the_inception_to_date_total(self):
        # The V1 defect in one case: on a single month, or even a fiscal year,
        # this account reads as a large false variance.
        account = self._account('1100', 'Bank', 'ASSET', 'BANK')
        self._ledger(account, 2019, 3, '5000')   # years before the fiscal year
        self._ledger(account, 2026, 7, '300')
        self._ledger(account, 2026, 8, '200')
        self._line(account, '5500')

        row = self._row(build_reconciliation(self.tenant), '1100')

        self.assertEqual(row['basis'], BASIS_INCEPTION)
        self.assertEqual(row['ledgerValue'], Decimal('5500'))
        self.assertEqual(row['status'], STATUS_MATCHED)

    def test_the_two_classes_are_not_given_the_same_basis(self):
        """Equal ledger history, equal Xero value — different correct answers."""
        revenue = self._account('4001', 'Sales', 'REVENUE', 'REVENUE')
        equity = self._account('9000', 'Share capital', 'EQUITY', 'EQUITY')
        for account in (revenue, equity):
            self._ledger(account, 2019, 3, '5000')
            self._ledger(account, 2026, 8, '100')
            self._line(account, '5100')

        result = build_reconciliation(self.tenant)

        # Balance sheet reaches back and agrees; P&L does not count prior years.
        self.assertEqual(self._row(result, '9000')['status'], STATUS_MATCHED)
        self.assertEqual(self._row(result, '4001')['ledgerValue'], Decimal('100'))
        self.assertEqual(self._row(result, '4001')['status'], STATUS_VARIANCE)

    def test_a_real_disagreement_is_reported_as_a_variance(self):
        account = self._account('5000', 'Rent', 'EXPENSE', 'EXPENSE')
        self._ledger(account, 2026, 8, '900')
        self._line(account, '1000')

        row = self._row(build_reconciliation(self.tenant), '5000')

        self.assertEqual(row['status'], STATUS_VARIANCE)
        self.assertEqual(row['variance'], Decimal('100'))

    def test_cent_rounding_is_tolerated_but_more_is_not(self):
        near = self._account('5001', 'Near', 'EXPENSE')
        far = self._account('5002', 'Far', 'EXPENSE')
        self._ledger(near, 2026, 8, '100.00')
        self._ledger(far, 2026, 8, '100.00')
        self._line(near, '100.01')
        self._line(far, '100.02')

        result = build_reconciliation(self.tenant)

        self.assertEqual(self._row(result, '5001')['status'], STATUS_MATCHED)
        self.assertEqual(self._row(result, '5002')['status'], STATUS_VARIANCE)

    def test_an_unclassifiable_account_is_declared_not_guessed(self):
        account = XeroAccount.objects.create(
            organisation=self.tenant, account_id='acc-odd', code='ODD',
            name='Mystery', type='SOMETHINGNEW', collection={},
        )
        self._line(account, '1000')

        row = self._row(build_reconciliation(self.tenant), 'ODD')

        self.assertEqual(row['status'], STATUS_UNCLASSIFIED)
        self.assertIsNone(row['basis'])
        self.assertIsNone(row['variance'])
        self.assertIn('basis is', row['reason'])

    def test_an_account_xero_reports_that_we_do_not_hold_is_named(self):
        self._line(None, '250', code='9999', name='Unknown to us')

        row = self._row(build_reconciliation(self.tenant), '9999')

        self.assertEqual(row['status'], STATUS_MISSING_IN_LEDGER)
        self.assertIn('9999', row['reason'])

    def test_a_ledger_balance_absent_from_xero_is_surfaced(self):
        account = self._account('1200', 'Receivables', 'ASSET', 'CURRENT')
        self._ledger(account, 2020, 1, '750')

        row = self._row(build_reconciliation(self.tenant), '1200')

        self.assertEqual(row['status'], STATUS_MISSING_IN_XERO)
        self.assertEqual(row['ledgerValue'], Decimal('750'))
        self.assertEqual(row['variance'], Decimal('-750'))

    def test_metrics_count_what_they_say_they_count(self):
        matched = self._account('5003', 'Matched', 'EXPENSE')
        varied = self._account('5004', 'Varied', 'EXPENSE')
        self._ledger(matched, 2026, 8, '100')
        self._ledger(varied, 2026, 8, '100')
        self._line(matched, '100')
        self._line(varied, '250')

        result = build_reconciliation(self.tenant)

        self.assertEqual(result['accountsCompared'], 2)
        self.assertEqual(result['reconciled'], 1)
        self.assertEqual(result['needsAttention'], 1)
        self.assertEqual(result['netVariance'], Decimal('150'))

    def test_no_imported_report_is_not_an_empty_reconciliation(self):
        """An empty table would read as 'everything agrees'."""
        XeroTrailBalanceReport.objects.all().delete()

        self.assertIsNone(build_reconciliation(self.tenant))
