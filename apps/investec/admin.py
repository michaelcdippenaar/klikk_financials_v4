from django.contrib import admin
from .models import (
    InvestecJseTransaction,
    InvestecJsePortfolio,
    InvestecJseShareNameMapping,
    InvestecJseShareMonthlyPerformance,
    InvestecBankAccount,
    InvestecBankTransaction,
)


@admin.register(InvestecJseTransaction)
class InvestecJseTransactionAdmin(admin.ModelAdmin):
    list_display = ['date', 'year', 'month', 'day', 'account_number', 'share_name', 'type', 'quantity', 'value', 'value_per_share', 'value_calculated', 'created_at']
    list_filter = ['date', 'year', 'month', 'type', 'account_number']
    search_fields = ['account_number', 'share_name', 'description']
    date_hierarchy = 'date'


@admin.register(InvestecJsePortfolio)
class InvestecJsePortfolioAdmin(admin.ModelAdmin):
    list_display = ['date', 'year', 'month', 'day', 'company', 'share_code', 'quantity', 'currency', 'unit_cost', 'total_cost', 'price', 'total_value', 'profit_loss']
    list_filter = ['date', 'year', 'month', 'currency', 'company']
    search_fields = ['company', 'share_code']
    date_hierarchy = 'date'
    readonly_fields = ['created_at', 'updated_at']


@admin.register(InvestecJseShareNameMapping)
class InvestecJseShareNameMappingAdmin(admin.ModelAdmin):
    list_display = ['share_name', 'share_name2', 'share_name3', 'company', 'share_code', 'created_at', 'updated_at']
    list_filter = ['company']
    search_fields = ['share_name', 'share_name2', 'share_name3', 'company', 'share_code']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(InvestecJseShareMonthlyPerformance)
class InvestecJseShareMonthlyPerformanceAdmin(admin.ModelAdmin):
    list_display = ['share_name', 'date', 'year', 'month', 'dividend_type', 'investec_account', 'dividend_ttm', 'closing_price', 'quantity', 'total_market_value', 'dividend_yield', 'created_at', 'updated_at']
    list_filter = ['date', 'year', 'month', 'share_name', 'dividend_type', 'investec_account']
    search_fields = ['share_name']
    date_hierarchy = 'date'
    readonly_fields = ['created_at', 'updated_at']


class InvestecBankTransactionInline(admin.TabularInline):
    model = InvestecBankTransaction
    extra = 0
    can_delete = True
    show_change_link = True
    ordering = ['-posting_date', '-posted_order']
    readonly_fields = [
        'type', 'transaction_type', 'status', 'description', 'card_number',
        'posted_order', 'posting_date', 'value_date', 'action_date', 'transaction_date',
        'amount', 'running_balance', 'uuid', 'created_at', 'updated_at',
    ]
    fields = [
        'posting_date', 'type', 'amount', 'transaction_type', 'status',
        'description', 'running_balance', 'uuid',
    ]
    max_num = 500


@admin.register(InvestecBankAccount)
class InvestecBankAccountAdmin(admin.ModelAdmin):
    list_display = [
        'account_number',
        'account_name',
        'reference_name',
        'product_name',
        'kyc_compliant',
        'profile_name',
        'created_at',
    ]
    list_filter = ['kyc_compliant']
    search_fields = ['account_id', 'account_number', 'account_name', 'reference_name']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [InvestecBankTransactionInline]


@admin.register(InvestecBankTransaction)
class InvestecBankTransactionAdmin(admin.ModelAdmin):
    list_display = [
        'posting_date',
        'account',
        'type',
        'amount',
        'transaction_type',
        'status',
        'description',
        'running_balance',
        'uuid',
    ]
    list_filter = ['account', 'type', 'status', 'transaction_type', 'posting_date']
    search_fields = ['description', 'uuid']
    date_hierarchy = 'posting_date'
    readonly_fields = ['created_at', 'updated_at']
    raw_id_fields = ['account']
