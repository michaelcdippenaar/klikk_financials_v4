from datetime import date
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient

from .models import InvestecJsePortfolio


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
        self.assertIn('results', data)
        first = data['results'][0]
        for field in ['id', 'date', 'company', 'share_code', 'quantity', 'price', 'total_value']:
            self.assertIn(field, first)

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
