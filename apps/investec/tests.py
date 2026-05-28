from datetime import date
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient

from .models import InvestecBankAccount, InvestecBankTransaction, InvestecJsePortfolio, InvestecJseTransaction


def make_portfolio(company='Test Co', share_code='TST', portfolio_date=None):
    """Helper: create one InvestecJsePortfolio row."""
    if portfolio_date is None:
        portfolio_date = date(2024, 1, 31)
    return InvestecJsePortfolio.objects.create(
        date=portfolio_date,
        company=company,
        share_code=share_code,
        quantity=Decimal('100.0000'),
        currency='ZAR',
        unit_cost=Decimal('10.0000'),
        total_cost=Decimal('1000.00'),
        price=Decimal('12.0000'),
        total_value=Decimal('1200.00'),
        portfolio_percent=Decimal('20.0000'),
    )


class PortfolioListViewTests(TestCase):
    """Tests for GET /api/investec/portfolio/"""

    def setUp(self):
        self.client = APIClient()
        for i in range(1, 6):
            make_portfolio(company=f'Company {i}', share_code=f'CO{i}')

    def test_list_returns_200(self):
        response = self.client.get('/api/investec/portfolio/')
        self.assertEqual(response.status_code, 200)

    def test_list_returns_all_rows(self):
        response = self.client.get('/api/investec/portfolio/')
        data = response.json()
        self.assertEqual(data['count'], 5)
        self.assertEqual(len(data['results']), 5)

    def test_response_shape(self):
        response = self.client.get('/api/investec/portfolio/')
        data = response.json()
        self.assertIn('count', data)
        self.assertIn('limit', data)
        self.assertIn('offset', data)
        self.assertIn('coverage', data)
        self.assertIn('results', data)
        first = data['results'][0]
        for field in ['id', 'date', 'company', 'share_code', 'quantity', 'price', 'total_value']:
            self.assertIn(field, first)

    def test_response_includes_missing_month_coverage(self):
        make_portfolio(company='March Company', share_code='MAR', portfolio_date=date(2024, 3, 31))
        response = self.client.get('/api/investec/portfolio/')
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['coverage']['first_month'], '2024-01')
        self.assertEqual(data['coverage']['last_month'], '2024-03')
        self.assertEqual(data['coverage']['expected_month_count'], 3)
        self.assertEqual(data['coverage']['present_month_count'], 2)
        self.assertEqual(data['coverage']['missing_month_count'], 1)
        self.assertEqual(data['coverage']['missing_months'][0]['month'], '2024-02')

    def test_year_filter(self):
        # Add a row for a different year
        make_portfolio(company='Old Company', share_code='OLD', portfolio_date=date(2023, 12, 31))
        response = self.client.get('/api/investec/portfolio/?year=2024')
        data = response.json()
        self.assertEqual(data['count'], 5)

    def test_limit_offset(self):
        response = self.client.get('/api/investec/portfolio/?limit=2&offset=0')
        data = response.json()
        self.assertEqual(data['count'], 5)
        self.assertEqual(len(data['results']), 2)
        self.assertEqual(data['limit'], 2)
        self.assertEqual(data['offset'], 0)

    def test_share_code_filter(self):
        response = self.client.get('/api/investec/portfolio/?share_code=CO1')
        data = response.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['share_code'], 'CO1')


class JseTransactionListViewCoverageTests(TestCase):
    """Tests for month coverage on GET /api/investec/transactions/"""

    def setUp(self):
        self.client = APIClient()
        InvestecJseTransaction.objects.create(
            date=date(2024, 1, 15),
            account_number='10011910139',
            description='Buy test share',
            share_name='TEST',
            type='Buy',
            quantity=Decimal('10.0000'),
            value=Decimal('-100.00'),
        )
        InvestecJseTransaction.objects.create(
            date=date(2024, 3, 20),
            account_number='10011910139',
            description='Dividend test share',
            share_name='TEST',
            type='Dividend',
            quantity=Decimal('0.0000'),
            value=Decimal('12.00'),
        )

    def test_response_includes_missing_month_coverage(self):
        response = self.client.get('/api/investec/transactions/')
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['coverage']['first_month'], '2024-01')
        self.assertEqual(data['coverage']['last_month'], '2024-03')
        self.assertEqual(data['coverage']['expected_month_count'], 3)
        self.assertEqual(data['coverage']['present_month_count'], 2)
        self.assertEqual(data['coverage']['missing_month_count'], 1)
        self.assertEqual(data['coverage']['missing_months'][0]['label'], 'Feb 2024')


class BankTransactionAccountFilterTests(TestCase):
    """Tests for account filtering on GET /api/investec/bank/transactions/"""

    def setUp(self):
        self.client = APIClient()
        self.account_a = InvestecBankAccount.objects.create(
            account_id='acc-a',
            account_number='10011910139',
            account_name='Mr MC Dippenaar',
        )
        self.account_b = InvestecBankAccount.objects.create(
            account_id='acc-b',
            account_number='10011924075',
            account_name='Klikk (Pty) Ltd',
        )
        self.account_c = InvestecBankAccount.objects.create(
            account_id='acc-c',
            account_number='10013017883',
            account_name='MLD Trust',
        )
        for idx, account in enumerate([self.account_a, self.account_b, self.account_c], start=1):
            InvestecBankTransaction.objects.create(
                account=account,
                type=InvestecBankTransaction.TYPE_DEBIT,
                status=InvestecBankTransaction.STATUS_POSTED,
                description=f'Test transaction {idx}',
                transaction_date=date(2026, 5, idx),
                amount=Decimal('10.00'),
            )

    def test_single_account_filter_still_works(self):
        response = self.client.get('/api/investec/bank/transactions/?account=10011910139')
        data = response.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['account_number'], '10011910139')

    def test_comma_separated_account_filter_returns_multiple_accounts(self):
        response = self.client.get('/api/investec/bank/transactions/?account=10011910139,10011924075')
        data = response.json()
        self.assertEqual(data['count'], 2)
        account_numbers = {row['account_number'] for row in data['results']}
        self.assertEqual(account_numbers, {'10011910139', '10011924075'})

    def test_repeated_account_filter_returns_multiple_accounts(self):
        response = self.client.get('/api/investec/bank/transactions/', {
            'account': ['10011910139', '10011924075'],
        })
        data = response.json()
        self.assertEqual(data['count'], 2)
        account_numbers = {row['account_number'] for row in data['results']}
        self.assertEqual(account_numbers, {'10011910139', '10011924075'})
