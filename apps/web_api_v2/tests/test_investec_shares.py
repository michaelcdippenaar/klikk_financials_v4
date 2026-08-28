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
BINDINGS = {'tenant-shares': ACCOUNT}

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

    def _execute(self):
        with patch('apps.web_api_v2.services.investec_shares.INVESTEC_SHARE_ENTITY_BINDINGS', BINDINGS):
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
        with patch('apps.web_api_v2.services.investec_shares.INVESTEC_SHARE_ENTITY_BINDINGS', BINDINGS):
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
