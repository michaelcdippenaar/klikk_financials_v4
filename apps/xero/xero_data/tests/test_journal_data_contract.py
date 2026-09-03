"""
The three data-contract properties of the journal mirror, pinned.

`excel_addin/README.md` calls these out as the ones that "silently corrupt
totals if ignored" — and none of them was tested, which is how the README and
`pivot_views.apply_journal_filters` came to disagree in the first place. Each
test here fails loudly on the plausible wrong implementation:

1. The legacy `journal` mirror restates entries the live transaction /
   manual_journal / system_journal feeds already carry, so including it doubles
   every figure. Both the pivot AND the search endpoint drop it when no
   journal_type is asked for; `journal_type=journal` is the deliberate way in.
2. `contact_id` is NULL on `journal` rows — the supplier lives on the source
   document. "Simplifying" the Coalesce back to `contact__name` loses the
   supplier on exactly the rows that only have one.
3. A journal balances within itself, so a cut that keeps both legs together
   nets to zero. That is arithmetic, not an empty result, and the pivot says so
   via `balancing_hint`.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import XeroJournals, XeroTransactionSource
from apps.xero.xero_metadata.models import XeroAccount, XeroContacts

User = get_user_model()

SEARCH = '/xero/data/journals/search/'
PIVOT = '/xero/data/journals/pivot/'


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=dt_timezone.utc)


class _Base(TestCase):
    """One tenant, two accounts, one supplier reachable only via the source.

    The ledger below is deliberately tiny and deliberately lopsided: the live
    feeds carry 1,000 of revenue against 1,000 of expense, and the frozen
    `journal` mirror restates the SAME entry. So the honest answer for expenses
    is 1,000 and the double-counted answer is 2,000 — a test that cannot pass
    by accident under either reading.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tenant = XeroTenant.objects.create(
            tenant_id='tenant-contract-1',
            tenant_name='Contract Test Co',
            fiscal_year_start_month=7,
        )
        cls.expense = XeroAccount.objects.create(
            organisation=cls.tenant, account_id='acc-expense',
            grouping='EXPENSE', code='400', name='Consulting', type='EXPENSE',
        )
        cls.revenue = XeroAccount.objects.create(
            organisation=cls.tenant, account_id='acc-revenue',
            grouping='REVENUE', code='200', name='Sales', type='REVENUE',
        )
        cls.supplier = XeroContacts.objects.create(
            organisation=cls.tenant, contacts_id='contact-1', name='Acme Supplies',
        )
        cls.source = XeroTransactionSource.objects.create(
            organisation=cls.tenant, transactions_id='txn-1',
            transaction_source='ACCPAY', contact=cls.supplier,
        )

        def line(journal_type, journal_id, number, account, amount,
                 source=None, contact=None):
            amount = Decimal(amount)
            return XeroJournals.objects.create(
                organisation=cls.tenant,
                journal_id=journal_id, journal_number=number,
                journal_type=journal_type,
                account=account, transaction_source=source, contact=contact,
                date=_dt(2025, 3, 14),
                description='consulting fee', reference='INV-1',
                amount=amount,
                debit=amount if amount > 0 else Decimal('0'),
                credit=amount if amount < 0 else Decimal('0'),
                tax_amount=Decimal('0'),
            )

        # Live feed: the real entry, both legs. The supplier is on the SOURCE,
        # never on the line — which is the shape 'journal' rows always have.
        cls.live_debit = line('transaction', 'live-1', 101, cls.expense, '1000.00',
                              source=cls.source)
        line('transaction', 'live-2', 101, cls.revenue, '-1000.00', source=cls.source)
        # Complementary live feed: a different entry, NOT a restatement.
        line('manual_journal', 'mj-1', 102, cls.expense, '250.00', contact=cls.supplier)
        line('manual_journal', 'mj-2', 102, cls.revenue, '-250.00', contact=cls.supplier)
        # Frozen legacy mirror: the SAME entry as live-1/live-2, own numbering.
        cls.mirror_debit = line('journal', 'mir-1', 900001, cls.expense, '1000.00',
                                source=cls.source)
        line('journal', 'mir-2', 900001, cls.revenue, '-1000.00', source=cls.source)
        line('journal', 'mir-3', 900002, cls.expense, '250.00', source=cls.source)
        line('journal', 'mir-4', 900002, cls.revenue, '-250.00', source=cls.source)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(
            User.objects.create_user(username='contract-tester', password='pw-not-logged')
        )

    def pivot(self, **params):
        params.setdefault('rows', 'account')
        params.setdefault('measure', 'amount')
        res = self.client.get(PIVOT, params)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    @staticmethod
    def row(data, label):
        for r in data['rows']:
            if not r['is_total'] and r['keys'][-1] == label:
                return r
        raise AssertionError('no row %r in %r' % (label, [r['keys'] for r in data['rows']]))


class DefaultExcludesLegacyMirrorTests(_Base):
    """Property 1: the default cut is the live ledger, not the ledger twice."""

    EXPENSE = '400 — Consulting'

    def test_pivot_default_excludes_journal_mirror(self):
        # Live feeds only: 1000 (transaction) + 250 (manual_journal).
        self.assertEqual(self.row(self.pivot(), self.EXPENSE)['cells'][0], 1250.0)

    def test_search_default_excludes_journal_mirror(self):
        res = self.client.get(SEARCH, {'limit': 1000})
        self.assertEqual(res.status_code, 200)
        types = {r['journal_type'] for r in res.data['results']}
        self.assertNotIn('journal', types)
        self.assertEqual(types, {'transaction', 'manual_journal'})
        self.assertEqual(res.data['count'], 4)
        self.assertTrue(res.data['mirror_excluded'])

    def test_search_and_pivot_agree_on_the_default_total(self):
        """The regression that motivated this file: search fed a PivotTable
        that disagreed with the server-side pivot on the same figure."""
        res = self.client.get(SEARCH, {'limit': 1000, 'account': '400'})
        self.assertEqual(res.status_code, 200)
        search_total = sum(Decimal(r['amount']) for r in res.data['results'])
        self.assertEqual(float(search_total),
                         self.row(self.pivot(), self.EXPENSE)['cells'][0])

    def test_journal_type_journal_is_the_deliberate_way_in(self):
        data = self.pivot(journal_type='journal')
        self.assertEqual(self.row(data, self.EXPENSE)['cells'][0], 1250.0)
        self.assertIsNotNone(data['mirror_hint'])

        res = self.client.get(SEARCH, {'limit': 1000, 'journal_type': 'journal'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual({r['journal_type'] for r in res.data['results']}, {'journal'})
        self.assertEqual(res.data['count'], 4)
        self.assertFalse(res.data['mirror_excluded'])

    def test_explicit_live_type_is_not_widened_by_the_default(self):
        res = self.client.get(SEARCH, {'limit': 1000, 'journal_type': 'manual_journal'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual({r['journal_type'] for r in res.data['results']}, {'manual_journal'})

    def test_summing_the_mirror_alongside_the_live_feeds_would_double(self):
        """Pins WHY the default exists: the raw table really does hold the
        expense twice, so an endpoint that returned everything would report
        2500 where the ledger says 1250."""
        everything = sum(
            j.amount for j in XeroJournals.objects.filter(account=self.expense)
        )
        self.assertEqual(everything, Decimal('2500.00'))


class SupplierViaSourceTests(_Base):
    """Property 2: the supplier is resolved through the source document."""

    def test_search_resolves_supplier_from_source_when_contact_is_null(self):
        res = self.client.get(SEARCH, {'limit': 1000, 'journal_type': 'transaction'})
        self.assertEqual(res.status_code, 200)
        row = next(r for r in res.data['results'] if r['account_code'] == '400')
        self.assertEqual(row['contact_name'], '')          # nothing on the line
        self.assertEqual(row['supplier_name'], 'Acme Supplies')
        self.assertEqual(row['supplier_via'], 'source')

    def test_search_prefers_the_line_contact_when_it_has_one(self):
        res = self.client.get(SEARCH, {'limit': 1000, 'journal_type': 'manual_journal'})
        self.assertEqual(res.status_code, 200)
        row = next(r for r in res.data['results'] if r['account_code'] == '400')
        self.assertEqual(row['supplier_name'], 'Acme Supplies')
        self.assertEqual(row['supplier_via'], 'journal')

    def test_pivot_supplier_dimension_uses_the_same_coalesce(self):
        """A 'transaction' row carries no contact of its own. If the Coalesce at
        pivot_views.DIMENSIONS['supplier'] were simplified to contact__name it
        would land under '(none)' instead of the supplier."""
        data = self.pivot(rows='supplier', journal_type='transaction', measure='debit')
        labels = {r['keys'][-1] for r in data['rows']}
        self.assertIn('Acme Supplies', labels)
        self.assertNotIn('(none)', labels)
        self.assertEqual(self.row(data, 'Acme Supplies')['cells'][0], 1000.0)

    def test_contact_filter_matches_through_the_source(self):
        res = self.client.get(SEARCH, {'limit': 1000, 'contact': 'Acme'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 4)


class BalancingHintTests(_Base):
    """Property 3: an all-zero cut is arithmetic, and says so."""

    def test_supplier_only_cut_nets_to_zero_and_is_counted(self):
        data = self.pivot(rows='supplier')
        self.assertEqual(self.row(data, 'Acme Supplies')['cells'][0], 0.0)
        self.assertTrue(data['zero_rows'])
        # The rows are on the sheet, showing their zeros, so there is nothing
        # to explain yet. The hint is for the case below.
        self.assertIsNone(data['balancing_hint'])

    def test_all_zero_cut_returns_the_hint_instead_of_an_empty_result(self):
        """The failure mode the hint exists for: suppress=1 drops every row and
        the sheet comes back empty, which reads as a broken tool rather than a
        correct answer unless the response says why."""
        data = self.pivot(rows='supplier', suppress='1')
        self.assertEqual(data['rows'], [])
        self.assertEqual(data['leaf_count'], 0)
        self.assertIsNotNone(data['balancing_hint'])
        self.assertIn('zero', data['balancing_hint'])

    def test_no_hint_once_a_separator_splits_the_legs(self):
        """Account on an axis keeps the legs apart, so the zeros are gone and
        the hint would be misleading."""
        data = self.pivot(rows='supplier,account', suppress='1')
        self.assertTrue(data['rows'])
        self.assertIsNone(data['balancing_hint'])

    def test_no_hint_on_a_measure_that_cannot_net_to_zero(self):
        data = self.pivot(rows='supplier', measure='debit', suppress='1')
        self.assertIsNone(data['balancing_hint'])
