"""Investec bank transactions for an entity.

Two things these hold to. An entity reaches only the accounts explicitly bound
to it — the binding is a reviewed cross-system mapping, never a name match, so
an unbound entity sees nothing rather than someone else's bank account. And a
full account number never reaches the browser: four digits tell accounts apart,
which is all the screen needs.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.investec.models import InvestecBankAccount, InvestecBankTransaction
from apps.web_api_v2.models import UserEntityMembership
from apps.web_api_v2.schema import schema
from apps.web_api_v2.services.investec_bank_transactions import (
    mask_account_number,
    read_bank_transactions,
)
from apps.xero.xero_core.models import XeroTenant

KLIKK_TENANT = '41ebfa0e-012e-4ff1-82ba-a9a7585c536c'
TEST_ACCOUNT = '10012345678'
# A stand-in owner map, so the test does not depend on which real accounts
# happen to be attributed to Klikk today.
TEST_OWNER_MAP = {TEST_ACCOUNT: {'entity': 'Klikk', 'capacity': 'business', 'liquid': True}}

QUERY = """
query B($context: FinancialContextInput!, $limit: Int!) {
  investecBankTransactions(context: $context, limit: $limit) {
    available userSafeReason totalCount
    accounts { maskedNumber name }
    summary { transactionCount netAmount earliestDate latestDate }
    rows { id date accountMasked description amount runningBalance }
  }
}
"""


class _Request:
    def __init__(self, user):
        self.user = user
        self.graphql_correlation_id = 'test'


class InvestecBankTransactionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='bank-viewer', email='bank@example.com', password='irrelevant',
        )
        self.klikk = XeroTenant.objects.create(
            tenant_id=KLIKK_TENANT, tenant_name='Klikk (Pty) Ltd', fiscal_year_start_month=7,
        )
        self.other = XeroTenant.objects.create(
            tenant_id='tenant-unbound', tenant_name='Unbound Co', fiscal_year_start_month=7,
        )
        for tenant in (self.klikk, self.other):
            UserEntityMembership.objects.create(
                user=self.user, entity=tenant, role='VIEWER', active=True,
            )

    def _account(self, number='10012345678', name='Business Current'):
        return InvestecBankAccount.objects.create(
            account_number=number, account_name=name,
        )

    def _txn(self, account, **kwargs):
        defaults = {
            'account': account, 'type': 'DEBIT', 'status': 'POSTED',
            'description': 'INVESTEC BUSINESS BANKING FEE',
            'transaction_date': date(2025, 8, 15), 'amount': Decimal('-295.00'),
            'running_balance': Decimal('1000.00'),
        }
        defaults.update(kwargs)
        return InvestecBankTransaction.objects.create(**defaults)

    def _execute(self, tenant, limit=100):
        with patch('apps.web_api_v2.services.investec_bank_transactions.INVESTEC_OWNER_MAP',
                   TEST_OWNER_MAP):
            return self._run(tenant, limit)

    def _run(self, tenant, limit):
        return schema.execute_sync(
            QUERY,
            variable_values={
                'context': {
                    'entityId': tenant.tenant_id, 'financialYear': 2026,
                    'periodSelection': {'mode': 'ALL', 'months': []},
                },
                'limit': limit,
            },
            context_value=type('Ctx', (), {'request': _Request(self.user)})(),
        )

    def test_an_unbound_entity_sees_nothing_rather_than_another_entitys_bank(self):
        payload = self._execute(self.other).data['investecBankTransactions']

        self.assertFalse(payload['available'])
        self.assertIn('No Investec bank account is bound', payload['userSafeReason'])
        self.assertEqual(payload['rows'], [])
        self.assertEqual(payload['accounts'], [])

    def test_a_bound_entity_with_no_synced_account_says_which_problem_it_is(self):
        # Bound-but-unsynced is a different problem from unbound, and needs a
        # different action.
        payload = self._execute(self.klikk).data['investecBankTransactions']

        self.assertFalse(payload['available'])
        self.assertIn('no matching bank account has been synced', payload['userSafeReason'].lower())

    def test_a_full_account_number_never_reaches_the_browser(self):
        account = self._account(number='10012345678')
        self._txn(account)

        payload = self._execute(self.klikk).data['investecBankTransactions']

        self.assertNotIn('10012345678', str(payload))
        self.assertEqual(payload['accounts'][0]['maskedNumber'], '•••• 5678')
        self.assertEqual(payload['rows'][0]['accountMasked'], '•••• 5678')

    def test_masking_does_not_invent_digits_it_does_not_have(self):
        self.assertEqual(mask_account_number('12'), '•••• ????')
        self.assertEqual(mask_account_number(None), '•••• ????')
        self.assertEqual(mask_account_number('GB29 NWBK 6016 1331 9268 19'), '•••• 6819')

    def test_only_transactions_inside_the_selected_period_are_returned(self):
        account = self._account()
        self._txn(account, transaction_date=date(2025, 8, 15))   # in FY2026
        self._txn(account, transaction_date=date(2024, 8, 15))   # a year earlier

        payload = self._execute(self.klikk).data['investecBankTransactions']

        self.assertEqual(payload['totalCount'], 1)
        self.assertEqual(payload['rows'][0]['date'], '2025-08-15')

    def test_the_summary_reports_the_whole_period_not_just_the_page(self):
        account = self._account()
        for day in range(1, 6):
            self._txn(account, transaction_date=date(2025, 8, day), amount=Decimal('10.00'))

        payload = self._execute(self.klikk, limit=2).data['investecBankTransactions']

        self.assertEqual(len(payload['rows']), 2)
        # A page of 2 must not make the period look like it holds 2.
        self.assertEqual(payload['totalCount'], 5)
        self.assertEqual(payload['summary']['transactionCount'], 5)
        self.assertEqual(Decimal(payload['summary']['netAmount']), Decimal('50.00'))

    def test_rows_carry_no_validation_or_evidence_field(self):
        # No V2 contract checks a bank transaction against anything. An empty
        # column would read as "checked, nothing found".
        account = self._account()
        self._txn(account)

        result = self._execute(self.klikk)
        self.assertIsNone(result.errors, result.errors)
        row = result.data['investecBankTransactions']['rows'][0]

        self.assertNotIn('validation', row)
        self.assertNotIn('evidence', row)

    def test_a_non_member_cannot_read_the_bank_transactions(self):
        UserEntityMembership.objects.filter(user=self.user, entity=self.klikk).update(active=False)

        result = self._execute(self.klikk)

        self.assertTrue(result.errors)
        self.assertIn(result.errors[0].extensions['code'], {'FORBIDDEN_ENTITY', 'FORBIDDEN'})

    def test_the_page_size_is_capped(self):
        account = self._account()
        self._txn(account)

        with patch('apps.web_api_v2.services.investec_bank_transactions.INVESTEC_OWNER_MAP',
                   TEST_OWNER_MAP):
            result = read_bank_transactions(self.klikk.pk, ['2025-08'], limit=10_000)

        self.assertLessEqual(len(result['rows']), 500)
