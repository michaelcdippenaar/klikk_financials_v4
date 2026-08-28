"""The reconciliation GraphQL read: access, honesty, and separated counts."""
from datetime import date
from unittest.mock import patch
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.web_api_v2.models import UserEntityCapability, UserEntityMembership
from apps.web_api_v2.schema import schema
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_cube.models import XeroTrailBalance
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_validation.models import (
    XeroTrailBalanceReport,
    XeroTrailBalanceReportLine,
)

QUERY = """
query R($context: FinancialContextInput!) {
  xeroReconciliation(context: $context) {
    available
    userSafeReason
    reportDate
    toleranceable: tolerance
    summary {
      accountsCompared reconciled needsAttention netVariance
      varianceCount missingInLedgerCount missingInXeroCount unclassifiedCount
    }
    rows { accountCode accountClass basis xeroValue ledgerValue variance status userSafeReason }
  }
}
"""


class _Request:
    def __init__(self, user):
        self.user = user
        self.graphql_correlation_id = 'test-correlation'


class ReconciliationQueryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='viewer', email='viewer@example.com', password='irrelevant',
        )
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-q', tenant_name='Query Co', fiscal_year_start_month=7,
        )
        self.membership = UserEntityMembership.objects.create(
            user=self.user, entity=self.tenant, role='VIEWER', active=True,
        )
        UserEntityCapability.objects.create(
            membership=self.membership, code='VIEW_FINANCIALS', active=True,
        )

    def _execute(self):
        return schema.execute_sync(
            QUERY,
            variable_values={'context': {
                'entityId': self.tenant.tenant_id,
                'financialYear': 2026,
                'periodSelection': {'mode': 'ALL', 'months': []},
            }},
            context_value=type('Ctx', (), {'request': _Request(self.user)})(),
        )

    def _seed(self):
        report = XeroTrailBalanceReport.objects.create(
            organisation=self.tenant, report_date=date(2026, 8, 31),
        )
        account = XeroAccount.objects.create(
            organisation=self.tenant, account_id='acc-1', code='5000',
            name='Rent', type='EXPENSE', collection={'Class': 'EXPENSE'},
        )
        XeroTrailBalance.objects.create(
            organisation=self.tenant, account=account, date=date(2026, 8, 1),
            year=2026, month=8, fin_year=2027, fin_period=2,
            amount=Decimal('900'), debit=Decimal('900'), credit=Decimal('0'),
        )
        XeroTrailBalanceReportLine.objects.create(
            report=report, account=account, account_code='5000',
            account_name='Rent', value=Decimal('1000'), row_type='Row',
        )

    def test_no_imported_report_says_so_rather_than_returning_an_empty_table(self):
        result = self._execute()

        self.assertIsNone(result.errors)
        payload = result.data['xeroReconciliation']
        self.assertFalse(payload['available'])
        self.assertIn('No Xero trial balance report', payload['userSafeReason'])
        self.assertEqual(payload['rows'], [])
        self.assertIsNone(payload['summary'])

    def test_a_variance_is_reported_with_its_basis(self):
        self._seed()

        payload = self._execute().data['xeroReconciliation']

        self.assertTrue(payload['available'])
        self.assertEqual(payload['reportDate'], '2026-08-31')
        row = payload['rows'][0]
        self.assertEqual(row['status'], 'VARIANCE')
        self.assertEqual(row['basis'], 'FISCAL_YEAR_TO_DATE')
        self.assertEqual(row['accountClass'], 'EXPENSE')
        self.assertEqual(Decimal(row['variance']), Decimal('100'))

    def test_counts_are_separated_by_kind_not_lumped_together(self):
        # A structural asymmetry must be distinguishable from a disagreement.
        self._seed()
        orphan = XeroAccount.objects.create(
            organisation=self.tenant, account_id='acc-2', code='1200',
            name='Receivables', type='CURRENT', collection={'Class': 'ASSET'},
        )
        XeroTrailBalance.objects.create(
            organisation=self.tenant, account=orphan, date=date(2020, 1, 1),
            year=2020, month=1, fin_year=2020, fin_period=7,
            amount=Decimal('750'), debit=Decimal('750'), credit=Decimal('0'),
        )

        summary = self._execute().data['xeroReconciliation']['summary']

        self.assertEqual(summary['varianceCount'], 1)
        self.assertEqual(summary['missingInXeroCount'], 1)
        self.assertEqual(summary['needsAttention'], 2)
        # accountsCompared counts only rows that were actually compared.
        self.assertEqual(summary['accountsCompared'], 1)

    def test_view_access_follows_membership_not_an_explicit_grant(self):
        # capability_codes_for_membership prepends VIEW_FINANCIALS for every
        # active member, so revoking the explicit grant changes nothing. The
        # real gate is membership, pinned by the next test. Recorded here so a
        # future reader does not mistake the belt-and-braces check for the gate.
        UserEntityCapability.objects.filter(membership=self.membership).update(active=False)
        self._seed()

        result = self._execute()

        self.assertIsNone(result.errors)
        self.assertTrue(result.data['xeroReconciliation']['available'])

    def test_a_non_member_cannot_read_another_entity(self):
        UserEntityMembership.objects.filter(pk=self.membership.pk).update(active=False)

        result = self._execute()

        self.assertTrue(result.errors)
        self.assertIn(result.errors[0].extensions['code'], {'FORBIDDEN_ENTITY', 'FORBIDDEN'})


ACCOUNT_QUERY = """
query A($context: FinancialContextInput!, $accountId: String!) {
  xeroReconciliationAccount(context: $context, accountId: $accountId) {
    accountCode accountName reportingGroup reportingLine truncated limit
    comparisonNote
    lines { id date reference description source ledgerValue }
  }
}
"""


class ReconciliationAccountQueryTests(ReconciliationQueryTests):
    """Drill-down from a variance into the ledger entries behind it."""

    def _execute_account(self, account_id='acc-1'):
        return schema.execute_sync(
            ACCOUNT_QUERY,
            variable_values={
                'context': {
                    'entityId': self.tenant.tenant_id,
                    'financialYear': 2026,
                    'periodSelection': {'mode': 'ALL', 'months': []},
                },
                'accountId': account_id,
            },
            context_value=type('Ctx', (), {'request': _Request(self.user)})(),
        )

    def _journal(self, account, **kwargs):
        from apps.xero.xero_data.models import XeroJournals
        defaults = {
            'organisation': self.tenant, 'account': account,
            'journal_id': f'j-{XeroJournals.objects.count() + 1}',
            'journal_number': 100 + XeroJournals.objects.count(),
            'date': date(2026, 8, 10), 'description': 'Rent for August',
            'reference': 'INV-1', 'amount': Decimal('900'),
            'tax_amount': Decimal('0'),
        }
        defaults.update(kwargs)
        return XeroJournals.objects.create(**defaults)

    def test_it_returns_the_ledger_entries_behind_an_account(self):
        self._seed()
        from apps.xero.xero_metadata.models import XeroAccount
        account = XeroAccount.objects.get(account_id='acc-1')
        account.grouping = 'EXPENSE'
        account.reporting_code_name = 'Operating expenses'
        account.save()
        self._journal(account)

        payload = self._execute_account().data['xeroReconciliationAccount']

        self.assertEqual(payload['accountCode'], '5000')
        self.assertEqual(payload['reportingGroup'], 'EXPENSE')
        self.assertEqual(payload['reportingLine'], 'Operating expenses')
        self.assertEqual(len(payload['lines']), 1)
        self.assertEqual(payload['lines'][0]['description'], 'Rent for August')
        self.assertEqual(Decimal(payload['lines'][0]['ledgerValue']), Decimal('900'))

    def test_it_says_why_there_is_no_xero_figure_per_line(self):
        # Xero's trial balance is account-level. Showing an empty Xero column
        # without saying so reads as "Xero reports nothing here", which is a
        # different and false claim.
        self._seed()
        payload = self._execute_account().data['xeroReconciliationAccount']

        self.assertIn('account-level report', payload['comparisonNote'])
        self.assertNotIn('xeroValue', payload['lines'][0] if payload['lines'] else {})

    def test_an_unknown_account_is_refused_rather_than_returned_empty(self):
        self._seed()

        result = self._execute_account(account_id='acc-does-not-exist')

        self.assertTrue(result.errors)
        self.assertEqual(result.errors[0].extensions['code'], 'NOT_FOUND')

    def test_a_long_history_is_truncated_and_says_so(self):
        self._seed()
        from apps.xero.xero_metadata.models import XeroAccount
        from apps.web_api_v2.services import xero_reconciliation as service
        account = XeroAccount.objects.get(account_id='acc-1')
        for index in range(5):
            self._journal(account, date=date(2026, 8, index + 1))

        with patch.object(service, 'ACCOUNT_LINE_LIMIT', 3):
            detail = service.account_lines(self.tenant, 'acc-1', limit=3)

        self.assertTrue(detail['truncated'])
        self.assertEqual(len(detail['lines']), 3)
