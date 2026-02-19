from rest_framework import serializers
from .models import InvestecJseTransaction, InvestecJsePortfolio, InvestecJseShareNameMapping, InvestecJseShareMonthlyPerformance


class InvestecJseTransactionSerializer(serializers.ModelSerializer):
    """Serializer for InvestecJseTransaction model."""
    
    class Meta:
        model = InvestecJseTransaction
        fields = [
            'id',
            'date',
            'account_number',
            'description',
            'share_name',
            'type',
            'quantity',
            'value',
            'value_per_share',
            'value_calculated',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InvestecJsePortfolioSerializer(serializers.ModelSerializer):
    """Serializer for InvestecJsePortfolio model."""
    
    class Meta:
        model = InvestecJsePortfolio
        fields = [
            'id',
            'date',
            'company',
            'share_code',
            'quantity',
            'currency',
            'unit_cost',
            'total_cost',
            'price',
            'total_value',
            'exchange_rate',
            'move_percent',
            'portfolio_percent',
            'profit_loss',
            'annual_income_zar',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InvestecJseShareNameMappingSerializer(serializers.ModelSerializer):
    """Serializer for InvestecJseShareNameMapping model."""
    
    class Meta:
        model = InvestecJseShareNameMapping
        fields = [
            'id',
            'share_name',
            'share_name2',
            'share_name3',
            'company',
            'share_code',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InvestecJseShareMonthlyPerformanceSerializer(serializers.ModelSerializer):
    """Serializer for InvestecJseShareMonthlyPerformance model."""
    
    class Meta:
        model = InvestecJseShareMonthlyPerformance
        fields = [
            'id',
            'share_name',
            'date',
            'year',
            'month',
            'dividend_type',
            'investec_account',
            'dividend_ttm',
            'closing_price',
            'quantity',
            'total_market_value',
            'dividend_yield',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

