from django.contrib import admin
from .models import (
    Symbol,
    PricePoint,
    Dividend,
    Split,
    SymbolInfo,
    FinancialStatement,
    EarningsReport,
    EarningsEstimate,
    AnalystRecommendation,
    AnalystPriceTarget,
    OwnershipSnapshot,
    NewsItem,
    DividendCalendar,
)


@admin.register(Symbol)
class SymbolAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'name', 'exchange', 'category', 'created_at', 'updated_at')
    search_fields = ('symbol', 'name')
    list_filter = ('category',)


@admin.register(PricePoint)
class PricePointAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'date', 'open', 'high', 'low', 'close', 'volume')
    list_filter = ('symbol', 'date')
    date_hierarchy = 'date'
    ordering = ('-date',)


@admin.register(Dividend)
class DividendAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'date', 'amount', 'currency', 'dividend_type')
    list_filter = ('symbol', 'dividend_type')
    date_hierarchy = 'date'


@admin.register(Split)
class SplitAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'date', 'ratio')
    list_filter = ('symbol',)
    date_hierarchy = 'date'


@admin.register(SymbolInfo)
class SymbolInfoAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'fetched_at')


@admin.register(FinancialStatement)
class FinancialStatementAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'statement_type', 'freq', 'period_end', 'fetched_at')
    list_filter = ('statement_type', 'freq')


@admin.register(EarningsReport)
class EarningsReportAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'freq', 'period_end', 'fetched_at')
    list_filter = ('freq',)


@admin.register(EarningsEstimate)
class EarningsEstimateAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'fetched_at')


@admin.register(AnalystRecommendation)
class AnalystRecommendationAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'fetched_at')


@admin.register(AnalystPriceTarget)
class AnalystPriceTargetAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'fetched_at')


@admin.register(OwnershipSnapshot)
class OwnershipSnapshotAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'holder_type', 'fetched_at')
    list_filter = ('holder_type',)


@admin.register(NewsItem)
class NewsItemAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'title', 'publisher', 'published_at')
    list_filter = ('symbol',)
    search_fields = ('title',)


@admin.register(DividendCalendar)
class DividendCalendarAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'declaration_date', 'ex_dividend_date', 'payment_date', 'amount', 'status', 'tm1_adjustment_written')
    list_filter = ('status', 'tm1_adjustment_written', 'symbol')
    date_hierarchy = 'ex_dividend_date'
    search_fields = ('symbol__symbol', 'symbol__name')
