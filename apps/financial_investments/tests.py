from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai_agent.models import KnowledgeCorpus, SystemDocument
from apps.financial_investments.models import NewsItem, Symbol
from apps.financial_investments.services_extra import fetch_news
from apps.investec.models import InvestecJsePortfolio, InvestecJseShareNameMapping, InvestecJseTransaction


class SymbolBuyTransactionsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.mapping = InvestecJseShareNameMapping.objects.create(
            share_name='KALGROUP',
            share_name2='KAL',
            company='KAL Group Limited',
            share_code='KAL',
        )
        self.symbol = Symbol.objects.create(
            symbol='KAL.JO',
            name='KAL Group Limited',
            share_name_mapping=self.mapping,
        )
        InvestecJseTransaction.objects.create(
            date=date(2026, 3, 12),
            account_number='10011910139',
            description='Buy KAL shares',
            share_name='KALGROUP',
            type='Buy',
            quantity=Decimal('100.0000'),
            value=Decimal('-18000.00'),
            value_per_share=Decimal('180.00'),
        )
        InvestecJseTransaction.objects.create(
            date=date(2026, 3, 13),
            account_number='10011910139',
            description='Sell KAL shares',
            share_name='KALGROUP',
            type='Sell',
            quantity=Decimal('50.0000'),
            value=Decimal('9500.00'),
            value_per_share=Decimal('190.00'),
        )
        InvestecJseTransaction.objects.create(
            date=date(2026, 3, 14),
            account_number='10011910139',
            description='Buy another mapped alias',
            share_name='KAL',
            type='Buy',
            quantity=Decimal('25.0000'),
            value=Decimal('-4600.00'),
            value_per_share=Decimal('184.00'),
        )

    def test_returns_buy_transactions_for_symbol_mapping_names(self):
        response = self.client.get('/api/financial-investments/symbols/KAL.JO/buy-transactions/')
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['symbol'], 'KAL.JO')
        self.assertEqual(data['share_code'], 'KAL')
        self.assertEqual(len(data['results']), 2)
        self.assertEqual({row['share_name'] for row in data['results']}, {'KALGROUP', 'KAL'})
        self.assertEqual({row['type'] for row in data['results']}, {'Buy'})

    def test_can_include_sell_transactions_for_chart_markers(self):
        response = self.client.get(
            '/api/financial-investments/symbols/KAL.JO/buy-transactions/?include_sells=1'
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data['results']), 3)
        self.assertEqual([row['type'] for row in data['results']], ['Buy', 'Sell', 'Buy'])

    def test_filters_buy_transactions_by_date_range(self):
        response = self.client.get(
            '/api/financial-investments/symbols/KAL.JO/buy-transactions/?start_date=2026-03-14'
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['date'], '2026-03-14')

    def test_uses_first_portfolio_holding_when_no_buy_transaction_exists(self):
        mapping = InvestecJseShareNameMapping.objects.create(
            share_name='BOXER',
            company='BOXER RETAIL LIMITED',
            share_code='BOX',
        )
        Symbol.objects.create(
            symbol='BOX.JO',
            name='Boxer Retail Ltd',
            share_name_mapping=mapping,
        )
        InvestecJsePortfolio.objects.create(
            date=date(2025, 5, 31),
            company='BOXER RETAIL LIMITED',
            share_code='BOX',
            quantity=Decimal('1549.0000'),
            unit_cost=Decimal('0.6449'),
            total_cost=Decimal('999.02'),
            price=Decimal('0.6584'),
            total_value=Decimal('1019.86'),
        )

        response = self.client.get('/api/financial-investments/symbols/BOX.JO/buy-transactions/')
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['source'], 'portfolio')
        self.assertEqual(data['results'][0]['date'], '2025-05-31')
        self.assertEqual(data['results'][0]['price'], 64.49)


class NewsFetchTests(TestCase):
    @patch('yfinance.Ticker')
    def test_fetch_news_parses_nested_yfinance_content_shape(self, ticker_cls):
        ticker_cls.return_value.get_news.return_value = [
            {
                'content': {
                    'title': 'Alexander Forbes headline',
                    'summary': 'A clean summary from Yahoo content.',
                    'pubDate': '2026-04-09T09:33:40Z',
                    'provider': {'displayName': 'Simply Wall St.'},
                    'canonicalUrl': {'url': 'https://finance.yahoo.com/example'},
                }
            }
        ]

        result = fetch_news('AFH.JO', count=1)
        item = NewsItem.objects.get(symbol__symbol='AFH.JO')

        self.assertEqual(result['created'], 1)
        self.assertEqual(item.title, 'Alexander Forbes headline')
        self.assertEqual(item.summary, 'A clean summary from Yahoo content.')
        self.assertEqual(item.publisher, 'Simply Wall St.')
        self.assertEqual(item.link, 'https://finance.yahoo.com/example')


class SymbolArticleVectorizationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.symbol = Symbol.objects.create(
            symbol='BOX.JO',
            name='Boxer Retail Ltd',
        )
        NewsItem.objects.create(
            symbol=self.symbol,
            title='Boxer sales growth accelerates',
            link='https://example.com/boxer-sales',
            published_at=timezone.now(),
            publisher='Example Markets',
            summary='Boxer reported stronger sales growth and improved store momentum.',
        )

    def test_prepares_stock_news_and_market_event_documents_without_embedding(self):
        response = self.client.post(
            '/api/financial-investments/symbols/BOX.JO/vectorize-articles/',
            {'vectorize': False},
            format='json',
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['symbol'], 'BOX.JO')
        self.assertEqual(data['stock_news_documents'], 1)
        self.assertEqual(data['market_event_documents'], 3)
        self.assertFalse(data['vectorized'])
        corpus = KnowledgeCorpus.objects.get(slug='financial-market-intelligence')
        docs = SystemDocument.objects.filter(corpus=corpus, metadata__symbol='BOX.JO')
        self.assertEqual(docs.count(), 4)
        self.assertTrue(docs.filter(metadata__type='market_event').exists())
        self.assertTrue(docs.filter(metadata__type='stock_news').exists())
