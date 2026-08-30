"""An entity's Investec share account.

This is the share ACCOUNT — the JSE stockbroking account loaded from Investec.
It is not the share analysis surface; market research and portfolio valuation
are a different thing and are not read here.

Two things these hold to. Holdings are a position at a date, so the read
returns the latest snapshot rather than summing snapshots across a period,
which would multiply the portfolio. And attribution is an explicit reviewed
binding, never a name match — binding an account attributes a real portfolio to
a real entity's books.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.investec.models import InvestecJsePortfolio, InvestecJseTransaction
from apps.web_api_v2.models import UserEntityMembership
from apps.web_api_v2.schema import schema
from apps.web_api_v2.services.investec_shares import read_share_account
from apps.xero.xero_core.models import XeroTenant

ACCOUNT = '10082386'

QUERY = """
query S($context: FinancialContextInput!) {
  investecShareAccount(context: $context) {
    available userSafeReason accountMasked holdingsAsAt holdingsValue transactionCount
    holdings { shareCode company quantity totalValue }
    unmappedShareNames
    summary { transactionCount netValue }
    transactions { date type shareName value }
  }
}
"""


class _Request:
    def __init__(self, user):
        self.user = user
        self.graphql_correlation_id = 'test'


class InvestecShareAccountTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='share-viewer', email='share@example.com', password='irrelevant',
        )
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-shares', tenant_name='Share Co', fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(
            user=self.user, entity=self.tenant, role='VIEWER', active=True,
        )

    def _holding(self, on, code='SNT', value='117321.07'):
        return InvestecJsePortfolio.objects.create(
            date=on, company='Santam Limited', share_code=code,
            quantity=Decimal('30131'), currency='ZAR',
            unit_cost=Decimal('3.00'), total_cost=Decimal('90000.00'),
            price=Decimal('3.89'), total_value=Decimal(value),
        )

    def _txn(self, on, **kwargs):
        defaults = {
            'date': on, 'account_number': ACCOUNT, 'description': 'Dividend received',
            'share_name': 'Santam', 'type': 'Dividend',
            'quantity': Decimal('0'), 'value': Decimal('19.20'),
        }
        defaults.update(kwargs)
        return InvestecJseTransaction.objects.create(**defaults)

    def _bind(self):
        from apps.investec.models import InvestecEntityAccount
        return InvestecEntityAccount.objects.create(
            entity=self.tenant, account_number=ACCOUNT,
            kind=InvestecEntityAccount.Kind.SHARE, active=True,
        )

    def _execute(self):
        self._bind()
        return self._run()

    def _run(self):
        return schema.execute_sync(
                QUERY,
                variable_values={'context': {
                    'entityId': self.tenant.tenant_id, 'financialYear': 2026,
                    'periodSelection': {'mode': 'ALL', 'months': []},
                }},
                context_value=type('Ctx', (), {'request': _Request(self.user)})(),
            )

    def test_an_unbound_entity_gets_no_portfolio(self):
        # Binding attributes a real portfolio to a real entity's books, so an
        # unbound entity must never inherit someone else's.
        result = schema.execute_sync(
            QUERY,
            variable_values={'context': {
                'entityId': self.tenant.tenant_id, 'financialYear': 2026,
                'periodSelection': {'mode': 'ALL', 'months': []},
            }},
            context_value=type('Ctx', (), {'request': _Request(self.user)})(),
        )
        payload = result.data['investecShareAccount']

        self.assertFalse(payload['available'])
        self.assertIn('No Investec share account is bound', payload['userSafeReason'])
        self.assertEqual(payload['holdings'], [])
        self.assertIsNone(payload['accountMasked'])

    def test_holdings_are_the_latest_snapshot_not_a_period_sum(self):
        # Two snapshots of the same holding must not be added together.
        self._holding(date(2026, 5, 31), value='100000.00')
        self._holding(date(2026, 6, 25), value='117321.07')
        self._txn(date(2025, 8, 10))

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['holdingsAsAt'], '2026-06-25')
        self.assertEqual(len(payload['holdings']), 1)
        self.assertEqual(Decimal(payload['holdingsValue']), Decimal('117321.07'))

    def test_transactions_are_scoped_to_the_selected_period(self):
        self._holding(date(2026, 6, 25))
        self._txn(date(2025, 8, 10))                       # inside FY2026
        self._txn(date(2024, 8, 10), value=Decimal('99'))  # a year earlier

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['transactionCount'], 1)
        self.assertEqual(payload['transactions'][0]['date'], '2025-08-10')
        self.assertEqual(Decimal(payload['summary']['netValue']), Decimal('19.20'))

    def test_the_account_number_is_masked(self):
        self._holding(date(2026, 6, 25))
        self._txn(date(2025, 8, 10))

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['accountMasked'], '•••• 2386')
        self.assertNotIn(ACCOUNT, str(payload))

    def test_another_accounts_transactions_are_not_included(self):
        self._holding(date(2026, 6, 25))
        self._txn(date(2025, 8, 10))
        self._txn(date(2025, 8, 11), account_number='99999999', value=Decimal('500'))

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['transactionCount'], 1)

    def test_a_bound_account_with_nothing_loaded_says_which_problem_it_is(self):
        self._bind()
        result = read_share_account(self.tenant.pk, ['2025-08'])

        self.assertFalse(result['available'])
        self.assertIn('no holdings or transactions have been loaded', result['userSafeReason'])

    def test_an_unmapped_share_name_is_reported_as_a_gap(self):
        # This account is loaded by uploading statements and mapping each share
        # name to a code. An unmapped name leaves its transactions
        # unattributable to a holding, so it must be surfaced, not discovered
        # later.
        from apps.investec.models import InvestecJseShareNameMapping

        self._holding(date(2026, 6, 25))
        self._txn(date(2025, 8, 10), share_name='Santam')
        self._txn(date(2025, 8, 11), share_name='Newly Listed Co')
        InvestecJseShareNameMapping.objects.create(share_name='Santam', share_code='SNT')

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['unmappedShareNames'], ['Newly Listed Co'])

    def test_account_level_rows_without_a_share_name_are_not_a_mapping_gap(self):
        self._holding(date(2026, 6, 25))
        self._txn(date(2025, 8, 10), share_name='', type='Fee', description='Broker fee')

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['unmappedShareNames'], [])

    def test_a_name_variant_counts_as_mapped(self):
        """One share arrives under several spellings on different statements.

        A mapping row carries up to three names for one share_code — "A V I"
        and "AVI" are the same instrument. Checking only the first name
        reported mapped shares as gaps, turning this screen into a source of
        false work.
        """
        from apps.investec.models import InvestecJseShareNameMapping

        self._holding(date(2026, 6, 25))
        self._txn(date(2025, 8, 10), share_name='AVI')
        self._txn(date(2025, 8, 11), share_name='JSE.AVI')
        InvestecJseShareNameMapping.objects.create(
            share_name='A V I', share_name2='AVI', share_name3='JSE.AVI', share_code='AVI',
        )

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['unmappedShareNames'], [])

    def test_a_variant_row_without_a_code_is_still_a_gap(self):
        # A name variant that resolves to no share_code resolves to nothing.
        from apps.investec.models import InvestecJseShareNameMapping

        self._holding(date(2026, 6, 25))
        self._txn(date(2025, 8, 10), share_name='JSE.AVI')
        InvestecJseShareNameMapping.objects.create(
            share_name='A V I', share_name2='JSE.AVI', share_code=None,
        )

        payload = self._execute().data['investecShareAccount']

        self.assertEqual(payload['unmappedShareNames'], ['JSE.AVI'])


class ShareTransactionPagingAndFilterTests(TestCase):
    """Paging, search and type filtering over an account's transactions.

    Two facts this holds apart. The period count is everything in the selected
    period; the filtered count is everything matching the current filter. They
    are different numbers, and reporting one as the other makes a search look
    like the account has shrunk.

    It also reads EVERY bound account. Investec renumbers a stockbroking
    account and leaves the history under the old number, so reading only the
    first bound account silently hid years of a portfolio behind a screen that
    looked like it was working.
    """

    OLD_ACCOUNT = '1812775'

    PAGED_QUERY = """
    query S($context: FinancialContextInput!, $limit: Int!, $offset: Int!,
            $search: String, $types: [String!]) {
      investecShareAccount(context: $context, limit: $limit, offset: $offset,
                           search: $search, types: $types) {
        available accountMasked transactionCount filteredCount transactionTypes
        transactions { date type shareName description value }
      }
    }
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='pager', password='irrelevant',
        )
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-pager', tenant_name='Pager Co', fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(
            user=self.user, entity=self.tenant, role='VIEWER', active=True,
        )

    def _bind(self, account_number=ACCOUNT):
        from apps.investec.models import InvestecEntityAccount
        return InvestecEntityAccount.objects.create(
            entity=self.tenant, account_number=account_number,
            kind=InvestecEntityAccount.Kind.SHARE, active=True,
        )

    def _txn(self, day, account=ACCOUNT, **kwargs):
        defaults = {
            'date': date(2026, 3, day), 'account_number': account,
            'description': 'Dividend received', 'share_name': 'Santam',
            'type': 'Dividend', 'quantity': Decimal('0'), 'value': Decimal('19.20'),
        }
        defaults.update(kwargs)
        return InvestecJseTransaction.objects.create(**defaults)

    def _run(self, **overrides):
        variables = {
            'context': {
                'entityId': self.tenant.tenant_id, 'financialYear': 2026,
                'periodSelection': {'mode': 'ALL', 'months': []},
            },
            'limit': 100, 'offset': 0, 'search': None, 'types': None,
        }
        variables.update(overrides)
        result = schema.execute_sync(
            self.PAGED_QUERY, variable_values=variables,
            context_value=type('C', (), {'request': _Request(self.user)})(),
        )
        self.assertIsNone(result.errors, result.errors)
        return result.data['investecShareAccount']

    def test_a_page_is_a_window_over_the_full_period_count(self):
        self._bind()
        for day in range(1, 8):
            self._txn(day)

        first = self._run(limit=3, offset=0)
        second = self._run(limit=3, offset=3)

        self.assertEqual(first['transactionCount'], 7)
        self.assertEqual(first['filteredCount'], 7)
        self.assertEqual(len(first['transactions']), 3)
        self.assertEqual(len(second['transactions']), 3)
        # Pages must not overlap, or a reader counts the same money twice.
        self.assertEqual(
            set(row['date'] for row in first['transactions'])
            & set(row['date'] for row in second['transactions']),
            set(),
        )

    def test_a_search_narrows_the_filtered_count_and_leaves_the_period_count_alone(self):
        self._bind()
        self._txn(1, share_name='Santam')
        self._txn(2, share_name='Kumba Iron Ore Ltd')
        self._txn(3, share_name='Kumba Iron Ore Ltd')

        result = self._run(search='kumba')

        self.assertEqual(result['filteredCount'], 2)
        # The account did not shrink because someone typed in a box.
        self.assertEqual(result['transactionCount'], 3)
        self.assertTrue(all('Kumba' in row['shareName'] for row in result['transactions']))

    def test_search_covers_the_fields_the_row_actually_shows(self):
        self._bind()
        self._txn(1, description='GROSS INTEREST 26/01/03', share_name='', type='Interest')
        self._txn(2, description='Dividend received', share_name='Santam', type='Dividend')

        self.assertEqual(self._run(search='gross interest')['filteredCount'], 1)
        self.assertEqual(self._run(search='santam')['filteredCount'], 1)
        self.assertEqual(self._run(search='dividend')['filteredCount'], 1)

    def test_type_filter_offers_only_types_that_are_present(self):
        self._bind()
        self._txn(1, type='Dividend')
        self._txn(2, type='TAX')
        self._txn(3, type='')

        result = self._run()

        # An option that returns nothing is a promise the screen cannot keep.
        self.assertEqual(result['transactionTypes'], ['Dividend', 'TAX'])
        self.assertEqual(self._run(types=['TAX'])['filteredCount'], 1)
        self.assertEqual(self._run(types=['TAX', 'Dividend'])['filteredCount'], 2)

    def test_every_bound_account_is_read_not_just_the_first(self):
        self._bind(ACCOUNT)
        self._bind(self.OLD_ACCOUNT)
        self._txn(1, account=ACCOUNT)
        self._txn(2, account=self.OLD_ACCOUNT)
        self._txn(3, account=self.OLD_ACCOUNT)

        result = self._run()

        self.assertEqual(result['transactionCount'], 3)
        # And the reader is told both accounts are in view, not just one.
        self.assertIn('2386', result['accountMasked'])
        self.assertIn('2775', result['accountMasked'])

    def test_an_unbound_account_contributes_nothing(self):
        self._bind(ACCOUNT)
        self._txn(1, account=ACCOUNT)
        self._txn(2, account='9999999')

        self.assertEqual(self._run()['transactionCount'], 1)
