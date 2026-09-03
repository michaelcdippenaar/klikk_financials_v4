"""
Build Trail Balance — scope derivation and the incremental SQL shape.

Scar tissue from 2026-09-03: an 80 s build of which 77 s was a DISTINCT that
leaked the model's default ordering (2,822 "periods" for 12 months, one DELETE
each and a 2,822-clause OR chain the planner could not index), plus every
build reprocessing all ~45k transaction journals because neither entry point
knew what had changed. These tests pin:

  * consolidate_journals deduplicates its period list, deletes in ONE
    statement and filters with a date range + ANY() instead of an OR chain;
  * derive_incremental_scope reads what changed (synced_at vs the reprocess
    stamp, pending manual journals, changed exclusion rules) and falls back to
    a full rebuild when it cannot bound the change;
  * process_xero_data forwards the derived periods to create_trail_balance;
  * fill_balance_sheet_gaps, given periods, only examines accounts touched in
    them but still fills each such account's whole series.
"""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_cube.models import XeroTrailBalance
from apps.xero.xero_cube.services import (
    derive_incremental_scope, fill_balance_sheet_gaps, process_xero_data,
)
from apps.xero.xero_data.models import (
    XeroJournalExclusion, XeroJournals, XeroJournalsSource, XeroTransactionSource,
)
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_sync.models import XeroLastUpdate


def _aware(y, m, d=15):
    return timezone.make_aware(dt.datetime(y, m, d, 12, 0))


class _Fixture(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(tenant_id='t-scope', tenant_name='Scope Co')
        self.bank = XeroAccount.objects.create(
            organisation=self.tenant, account_id='acc-bank', code='090', grouping='ASSET')
        self.sales = XeroAccount.objects.create(
            organisation=self.tenant, account_id='acc-sales', code='200', grouping='REVENUE')

    def _source(self, tid, synced_at=None):
        return XeroTransactionSource.objects.create(
            organisation=self.tenant, transactions_id=tid, transaction_source='Invoice',
            collection={}, synced_at=synced_at,
        )

    def _journal(self, jid, account, when, amount, source=None, journal_source=None,
                 journal_type='transaction', number=1):
        return XeroJournals.objects.create(
            organisation=self.tenant, journal_id=jid, journal_number=number,
            journal_type=journal_type, account=account, date=when,
            transaction_source=source, journal_source=journal_source,
            amount=Decimal(amount), tax_amount=Decimal('0'),
        )

    def _stamp(self, end_point, when):
        XeroLastUpdate.objects.update_or_create(
            end_point=end_point, organisation=self.tenant, defaults={'date': when})


class ConsolidateJournalsShapeTests(_Fixture):
    def setUp(self):
        super().setUp()
        src = self._source('tx-1', synced_at=timezone.now())
        # Two legs in Jan, one in Feb, one in Mar (untouched by the rebuild).
        self._journal('j1', self.bank, _aware(2025, 1), '100', source=src)
        self._journal('j2', self.sales, _aware(2025, 1), '-100', source=src)
        self._journal('j3', self.bank, _aware(2025, 2), '40', source=src)
        self._journal('j4', self.bank, _aware(2025, 3), '7', source=src)
        # Seed the TB with stale rows for Jan/Feb (to be replaced) and Mar (kept).
        for y, m, amt in ((2025, 1, '999'), (2025, 2, '999'), (2025, 3, '7')):
            XeroTrailBalance.objects.create(
                organisation=self.tenant, account=self.bank, date=dt.date(y, m, 1),
                year=y, month=m, fin_year=y, fin_period=m,
                amount=Decimal(amt), debit=Decimal(amt), credit=0, tax_amount=0)

    def test_duplicated_periods_collapse_to_one_delete_and_an_any_predicate(self):
        duplicated = [(2025, 1)] * 300 + [(2025, 2)] * 300 + [(2025, 1)]

        with CaptureQueriesContext(connection) as ctx:
            XeroTrailBalance.objects.consolidate_journals(self.tenant, affected_periods=duplicated)

        deletes = [q['sql'] for q in ctx.captured_queries if q['sql'].lstrip().upper().startswith('DELETE')]
        inserts = [q['sql'] for q in ctx.captured_queries if 'INSERT INTO xero_cube_xerotrailbalance' in q['sql']]
        self.assertEqual(len(deletes), 1, deletes)
        self.assertEqual(len(inserts), 1)
        self.assertIn('= ANY(', inserts[0])
        self.assertNotIn(' OR (EXTRACT(YEAR', inserts[0])
        # The list was deduplicated before it reached SQL: two periods, not 601.
        self.assertIn('ARRAY[202501,202502]', inserts[0].replace(' ', ''))

        rows = {(r.year, r.month, r.account_id): r.amount
                for r in XeroTrailBalance.objects.filter(organisation=self.tenant)}
        self.assertEqual(rows[(2025, 1, 'acc-bank')], Decimal('100'))
        self.assertEqual(rows[(2025, 1, 'acc-sales')], Decimal('-100'))
        self.assertEqual(rows[(2025, 2, 'acc-bank')], Decimal('40'))
        # March was outside the scope and must be untouched.
        self.assertEqual(rows[(2025, 3, 'acc-bank')], Decimal('7'))
        self.assertEqual(len(rows), 4)

    def test_non_contiguous_periods_do_not_rebuild_the_months_between(self):
        XeroTrailBalance.objects.consolidate_journals(self.tenant, affected_periods=[(2025, 1), (2025, 3)])
        feb = XeroTrailBalance.objects.get(organisation=self.tenant, year=2025, month=2)
        self.assertEqual(feb.amount, Decimal('999'), 'February was not in scope and must keep its stale row')
        jan = XeroTrailBalance.objects.get(organisation=self.tenant, year=2025, month=1, account=self.bank)
        self.assertEqual(jan.amount, Decimal('100'))


class DeriveIncrementalScopeTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.last_run = timezone.now() - dt.timedelta(hours=6)
        self._stamp('process_journals', self.last_run)
        self._stamp('trail_balance', self.last_run)

    def test_without_a_previous_build_it_asks_for_a_full_rebuild(self):
        XeroLastUpdate.objects.filter(organisation=self.tenant).delete()
        self.assertIsNone(derive_incremental_scope(self.tenant))

    def test_only_transactions_synced_after_the_last_reprocess_are_touched(self):
        old = self._source('tx-old', synced_at=self.last_run - dt.timedelta(days=1))
        new = self._source('tx-new', synced_at=self.last_run + dt.timedelta(minutes=1))
        for i in range(8):  # keep the touched ratio well under the full-rebuild threshold
            self._source(f'tx-filler-{i}', synced_at=self.last_run - dt.timedelta(days=2))
        self._journal('j-old', self.bank, _aware(2024, 5), '1', source=old)
        self._journal('j-new', self.bank, _aware(2025, 7), '1', source=new)

        scope = derive_incremental_scope(self.tenant)

        self.assertEqual(scope['touched_transaction_ids'], {'tx-new'})
        self.assertEqual(scope['affected_periods'], {(2025, 7)})
        self.assertEqual(scope['pending_journal_source_ids'], [])

    def test_a_re_dated_transaction_keeps_its_old_month_in_scope(self):
        # The journals still carry the OLD date at derivation time; that month
        # must be rebuilt too, or the stale movement stays in the trail balance.
        src = self._source('tx-moved', synced_at=timezone.now())
        for i in range(3):
            self._source(f'tx-static-{i}', synced_at=self.last_run - dt.timedelta(days=2))
        self._journal('j-moved', self.bank, _aware(2024, 11), '5', source=src)

        scope = derive_incremental_scope(self.tenant)
        self.assertEqual(scope['affected_periods'], {(2024, 11)})

    def test_most_of_the_ledger_touched_means_full_rebuild(self):
        # The synced_at backfill migration stamps every row "now": the first
        # build after deploy must do one honest full rebuild, not incremental
        # bookkeeping over 20k ids.
        for i in range(4):
            self._source(f'tx-{i}', synced_at=timezone.now())
        self.assertIsNone(derive_incremental_scope(self.tenant))

    def test_pending_manual_journals_add_their_current_months(self):
        js = XeroJournalsSource.objects.create(
            organisation=self.tenant, journal_id='mj-1', journal_number=77,
            journal_type='manual_journal', collection={}, processed=False)
        self._journal('mj-1_0', self.bank, _aware(2025, 2), '9', journal_source=js,
                      journal_type='manual_journal', number=77)

        scope = derive_incremental_scope(self.tenant)
        self.assertEqual(scope['pending_journal_source_ids'], [js.id])
        self.assertEqual(scope['affected_periods'], {(2025, 2)})

    def test_a_changed_dated_exclusion_adds_its_month(self):
        XeroJournalExclusion.objects.create(
            organisation=self.tenant, date=dt.date(2025, 4, 3), reason='test')
        scope = derive_incremental_scope(self.tenant)
        self.assertEqual(scope['affected_periods'], {(2025, 4)})

    def test_a_changed_exclusion_keyed_on_journal_number_adds_the_journal_months(self):
        self._journal('j-num', self.sales, _aware(2025, 6), '-3', number=4242)
        XeroJournalExclusion.objects.create(
            organisation=self.tenant, journal_number=4242, reason='test')
        scope = derive_incremental_scope(self.tenant)
        self.assertEqual(scope['affected_periods'], {(2025, 6)})

    def test_an_unchanged_exclusion_is_ignored(self):
        ex = XeroJournalExclusion.objects.create(
            organisation=self.tenant, date=dt.date(2025, 4, 3), reason='test')
        # auto_now cannot be set through create(); push updated_at into the past.
        XeroJournalExclusion.objects.filter(pk=ex.pk).update(
            updated_at=self.last_run - dt.timedelta(days=1))
        scope = derive_incremental_scope(self.tenant)
        self.assertEqual(scope['affected_periods'], set())

    def test_a_pattern_only_exclusion_cannot_be_bounded_so_full_rebuild(self):
        XeroJournalExclusion.objects.create(
            organisation=self.tenant, description='Prorata VAT', reason='test')
        self.assertIsNone(derive_incremental_scope(self.tenant))


class ProcessXeroDataScopeForwardingTests(_Fixture):
    @patch('apps.xero.xero_cube.services.calculate_balance_sheet_balance_to_date')
    @patch('apps.xero.xero_cube.services.create_trail_balance')
    @patch('apps.xero.xero_data.transaction_processor.process_transactions_to_journals')
    def test_derived_scope_reaches_both_steps(self, mock_txn, mock_tb, mock_ytd):
        last_run = timezone.now() - dt.timedelta(hours=1)
        self._stamp('process_journals', last_run)
        self._stamp('trail_balance', last_run)
        touched = self._source('tx-hot', synced_at=timezone.now())
        for i in range(5):
            self._source(f'tx-cold-{i}', synced_at=last_run - dt.timedelta(days=1))
        self._journal('j-hot', self.bank, _aware(2025, 9), '12', source=touched)
        mock_txn.return_value = {'journal_entries_created': 1}
        mock_tb.return_value = {'skipped': False, 'records': 1}

        result = process_xero_data(self.tenant.tenant_id)

        self.assertTrue(result['success'])
        self.assertEqual(result['stats']['scope'], 'incremental')
        self.assertEqual(mock_txn.call_args.kwargs['touched_transaction_ids'], {'tx-hot'})
        self.assertEqual(mock_tb.call_args.kwargs['affected_periods'], [(2025, 9)])
        self.assertEqual(mock_ytd.call_args.kwargs['affected_periods'], [(2025, 9)])
        self.assertEqual(result['stats']['affected_periods'], ['2025-09'])
        self.assertIn('trail_balance', result['stats']['step_seconds'])
        self.assertTrue(mock_tb.call_args.kwargs['incremental'])

    @patch('apps.xero.xero_cube.services.calculate_balance_sheet_balance_to_date')
    @patch('apps.xero.xero_cube.services.create_trail_balance')
    @patch('apps.xero.xero_data.transaction_processor.process_transactions_to_journals')
    def test_a_derived_full_scope_forces_a_full_trail_balance_rebuild(self, mock_txn, mock_tb, mock_ytd):
        # Production 2026-09-03: the synced_at backfill made every transaction
        # "touched", the journals were fully regenerated (7.9 s), and then the
        # trail balance step fell into the date-window fallback, rebuilt ONE
        # month and inserted nothing. A full reprocess needs a full rebuild.
        last_run = timezone.now() - dt.timedelta(hours=1)
        self._stamp('process_journals', last_run)
        self._stamp('trail_balance', last_run)
        for i in range(4):
            self._source(f'tx-{i}', synced_at=timezone.now())
        mock_txn.return_value = {'journal_entries_created': 4}
        mock_tb.return_value = {'skipped': False, 'records': 4}

        result = process_xero_data(self.tenant.tenant_id)

        self.assertEqual(result['stats']['scope'], 'full')
        self.assertIsNone(mock_txn.call_args.kwargs['touched_transaction_ids'])
        self.assertFalse(mock_tb.call_args.kwargs['incremental'])
        self.assertIsNone(mock_tb.call_args.kwargs['affected_periods'])
        self.assertIsNone(mock_ytd.call_args.kwargs['affected_periods'])

    @patch('apps.xero.xero_cube.services.calculate_balance_sheet_balance_to_date')
    @patch('apps.xero.xero_cube.services.create_trail_balance')
    @patch('apps.xero.xero_data.transaction_processor.process_transactions_to_journals')
    def test_rebuild_flag_ignores_any_scope(self, mock_txn, mock_tb, mock_ytd):
        mock_txn.return_value = {}
        mock_tb.return_value = {'skipped': False, 'records': 1}
        result = process_xero_data(self.tenant.tenant_id, rebuild_trail_balance=True,
                                   touched_transaction_ids={'tx-x'}, affected_periods=[(2025, 1)])
        self.assertEqual(result['stats']['scope'], 'full')
        self.assertIsNone(mock_txn.call_args.kwargs['touched_transaction_ids'])
        self.assertIsNone(mock_tb.call_args.kwargs['affected_periods'])


class GapFillScopeTests(_Fixture):
    def _tb(self, account, y, m, amount='1'):
        XeroTrailBalance.objects.create(
            organisation=self.tenant, account=account, date=dt.date(y, m, 1),
            year=y, month=m, fin_year=y, fin_period=m,
            amount=Decimal(amount), debit=Decimal(amount), credit=0, tax_amount=0)

    def test_only_accounts_touched_in_the_periods_are_filled_but_over_their_whole_series(self):
        other = XeroAccount.objects.create(
            organisation=self.tenant, account_id='acc-loan', code='800', grouping='LIABILITY')
        self._tb(self.bank, 2025, 1)
        self._tb(self.bank, 2025, 4)   # Feb + Mar are gaps
        self._tb(other, 2025, 6)
        self._tb(other, 2025, 8)       # July is a gap, but this account is out of scope

        inserted = fill_balance_sheet_gaps(self.tenant.tenant_id, affected_periods=[(2025, 4)])

        self.assertEqual(inserted, 2)
        bank_months = sorted(XeroTrailBalance.objects.filter(account=self.bank).values_list('month', flat=True))
        self.assertEqual(bank_months, [1, 2, 3, 4])
        self.assertFalse(XeroTrailBalance.objects.filter(account=other, month=7).exists())

    def test_a_month_whose_only_rows_were_gap_rows_is_refilled_after_the_rebuild_wiped_it(self):
        # Production 2026-09-03: rebuilding Sep-2025 deleted every Sep row,
        # zero gap rows included; a gap-fill restricted to accounts that
        # still HAD a Sep row skipped the 194 accounts whose only Sep rows
        # were gaps, and the balance sheet lost those months.
        self._tb(self.bank, 2025, 1)
        self._tb(self.bank, 2025, 3)
        self._tb(self.bank, 2025, 2, amount='0')          # the gap row
        XeroTrailBalance.objects.consolidate_journals(self.tenant, affected_periods=[(2025, 2)])
        self.assertFalse(XeroTrailBalance.objects.filter(account=self.bank, month=2).exists(),
                         'precondition: the incremental consolidate wipes the month')

        inserted = fill_balance_sheet_gaps(self.tenant.tenant_id, affected_periods=[(2025, 2)])

        self.assertEqual(inserted, 1)
        self.assertTrue(XeroTrailBalance.objects.filter(account=self.bank, month=2, amount=0).exists())

    def test_without_periods_every_balance_sheet_account_is_filled(self):
        self._tb(self.bank, 2025, 1)
        self._tb(self.bank, 2025, 3)
        self.assertEqual(fill_balance_sheet_gaps(self.tenant.tenant_id), 1)
        self.assertTrue(XeroTrailBalance.objects.filter(account=self.bank, month=2, amount=0).exists())
