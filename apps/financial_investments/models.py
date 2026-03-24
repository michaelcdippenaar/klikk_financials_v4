from django.db import models


class Symbol(models.Model):
    """Ticker symbol reference (e.g. AAPL, NED.JO). Links to Investec JSE mapping when applicable."""
    CATEGORY_EQUITY = 'equity'
    CATEGORY_ETF = 'etf'
    CATEGORY_INDEX = 'index'
    CATEGORY_FOREX = 'forex'
    CATEGORY_CHOICES = [
        (CATEGORY_EQUITY, 'Equity'),
        (CATEGORY_ETF, 'ETF'),
        (CATEGORY_INDEX, 'Index'),
        (CATEGORY_FOREX, 'Forex'),
    ]

    symbol = models.CharField(max_length=20, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    exchange = models.CharField(max_length=50, blank=True)
    category = models.CharField(max_length=20, blank=True, choices=CATEGORY_CHOICES, db_index=True)
    share_name_mapping = models.OneToOneField(
        'investec.InvestecJseShareNameMapping',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='financial_symbol',
        help_text='Link to Investec JSE share name mapping for additional share data (share_name, company, share_code).',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['symbol']
        verbose_name = 'Symbol'
        verbose_name_plural = 'Symbols'

    def __str__(self):
        return self.symbol


class PricePoint(models.Model):
    """Daily OHLCV (and optionally adjusted close) for a symbol."""
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='price_points', db_index=True)
    date = models.DateField(db_index=True)
    open = models.DecimalField(max_digits=18, decimal_places=4)
    high = models.DecimalField(max_digits=18, decimal_places=4)
    low = models.DecimalField(max_digits=18, decimal_places=4)
    close = models.DecimalField(max_digits=18, decimal_places=4)
    volume = models.BigIntegerField(null=True, blank=True)
    adjusted_close = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']
        verbose_name = 'Price point'
        verbose_name_plural = 'Price points'
        constraints = [
            models.UniqueConstraint(fields=['symbol', 'date'], name='financial_investments_symbol_date_unique'),
        ]
        indexes = [
            models.Index(fields=['symbol', 'date'], name='fi_symbol_date_idx'),
        ]

    def __str__(self):
        return f'{self.symbol.symbol} {self.date}'


class Dividend(models.Model):
    """Dividend payment per symbol per date (from yfinance get_dividends)."""
    TYPE_DECLARED = 'dividend_declared'
    TYPE_PAID = 'dividend_paid'
    TYPE_CHOICES = [
        (TYPE_DECLARED, 'Dividend declared'),
        (TYPE_PAID, 'Dividend paid'),
    ]

    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='dividends', db_index=True)
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=18, decimal_places=6)
    currency = models.CharField(max_length=10, blank=True)
    dividend_type = models.CharField(
        max_length=20, choices=TYPE_CHOICES,
        default=TYPE_PAID, db_index=True,
    )

    class Meta:
        ordering = ['-date']
        verbose_name = 'Dividend'
        verbose_name_plural = 'Dividends'
        constraints = [
            models.UniqueConstraint(fields=['symbol', 'date', 'dividend_type'], name='fi_dividend_symbol_date_type_unique'),
        ]

    def __str__(self):
        return f'{self.symbol.symbol} {self.date} {self.amount} ({self.get_dividend_type_display()})'


class Split(models.Model):
    """Stock split event (from yfinance get_splits). Ratio e.g. 2.0 = 2-for-1."""
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='splits', db_index=True)
    date = models.DateField(db_index=True)
    ratio = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        ordering = ['-date']
        verbose_name = 'Split'
        verbose_name_plural = 'Splits'
        constraints = [
            models.UniqueConstraint(fields=['symbol', 'date'], name='fi_split_symbol_date_unique'),
        ]

    def __str__(self):
        return f'{self.symbol.symbol} {self.date} {self.ratio}:1'


class SymbolInfo(models.Model):
    """Company/instrument info snapshot from yfinance ticker.info (JSONB)."""
    symbol = models.OneToOneField(
        Symbol, on_delete=models.CASCADE, related_name='info', db_index=True
    )
    fetched_at = models.DateTimeField(auto_now=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = 'Symbol info'
        verbose_name_plural = 'Symbol infos'

    def __str__(self):
        return f'{self.symbol.symbol} info'


class FinancialStatement(models.Model):
    """Income statement, balance sheet, or cash flow (JSONB)."""
    TYPE_INCOME = 'income_stmt'
    TYPE_BALANCE = 'balance_sheet'
    TYPE_CASHFLOW = 'cash_flow'
    TYPE_CHOICES = [
        (TYPE_INCOME, 'Income statement'),
        (TYPE_BALANCE, 'Balance sheet'),
        (TYPE_CASHFLOW, 'Cash flow'),
    ]
    FREQ_YEARLY = 'yearly'
    FREQ_QUARTERLY = 'quarterly'
    FREQ_TRAILING = 'trailing'
    FREQ_CHOICES = [(FREQ_YEARLY, 'Yearly'), (FREQ_QUARTERLY, 'Quarterly'), (FREQ_TRAILING, 'Trailing')]

    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='financial_statements', db_index=True)
    statement_type = models.CharField(max_length=20, choices=TYPE_CHOICES, db_index=True)
    period_end = models.DateField(null=True, blank=True, db_index=True)
    freq = models.CharField(max_length=20, choices=FREQ_CHOICES, default=FREQ_YEARLY)
    data = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-period_end']
        verbose_name = 'Financial statement'
        verbose_name_plural = 'Financial statements'
        constraints = [
            models.UniqueConstraint(
                fields=['symbol', 'statement_type', 'freq'],
                name='fi_stmt_symbol_type_freq_unique',
            ),
        ]

    def __str__(self):
        return f'{self.symbol.symbol} {self.statement_type} {self.period_end}'


class EarningsReport(models.Model):
    """Reported earnings (actuals) from yfinance get_earnings (JSONB)."""
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='earnings_reports', db_index=True)
    period_end = models.DateField(null=True, blank=True, db_index=True)
    freq = models.CharField(max_length=20)
    data = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-period_end']
        verbose_name = 'Earnings report'
        verbose_name_plural = 'Earnings reports'

    def __str__(self):
        return f'{self.symbol.symbol} earnings {self.period_end}'


class EarningsEstimate(models.Model):
    """Analyst earnings estimates (0q, +1q, 0y, +1y) from yfinance (JSONB)."""
    symbol = models.OneToOneField(
        Symbol, on_delete=models.CASCADE, related_name='earnings_estimate', db_index=True
    )
    fetched_at = models.DateTimeField(auto_now=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = 'Earnings estimate'
        verbose_name_plural = 'Earnings estimates'

    def __str__(self):
        return f'{self.symbol.symbol} earnings estimate'


class AnalystRecommendation(models.Model):
    """Analyst recommendations history (JSONB list)."""
    symbol = models.OneToOneField(
        Symbol, on_delete=models.CASCADE, related_name='analyst_recommendations', db_index=True
    )
    fetched_at = models.DateTimeField(auto_now=True)
    data = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = 'Analyst recommendation'
        verbose_name_plural = 'Analyst recommendations'

    def __str__(self):
        return f'{self.symbol.symbol} recommendations'


class AnalystPriceTarget(models.Model):
    """Analyst price targets (JSONB)."""
    symbol = models.OneToOneField(
        Symbol, on_delete=models.CASCADE, related_name='analyst_price_target', db_index=True
    )
    fetched_at = models.DateTimeField(auto_now=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = 'Analyst price target'
        verbose_name_plural = 'Analyst price targets'

    def __str__(self):
        return f'{self.symbol.symbol} price target'


class OwnershipSnapshot(models.Model):
    """Institutional holders, major holders, or insider transactions (JSONB)."""
    HOLDER_INSTITUTIONAL = 'institutional'
    HOLDER_MAJOR = 'major'
    HOLDER_INSIDER = 'insider_transactions'
    HOLDER_CHOICES = [
        (HOLDER_INSTITUTIONAL, 'Institutional'),
        (HOLDER_MAJOR, 'Major'),
        (HOLDER_INSIDER, 'Insider transactions'),
    ]

    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='ownership_snapshots', db_index=True)
    holder_type = models.CharField(max_length=30, choices=HOLDER_CHOICES, db_index=True)
    fetched_at = models.DateTimeField(auto_now=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-fetched_at']
        verbose_name = 'Ownership snapshot'
        verbose_name_plural = 'Ownership snapshots'
        constraints = [
            models.UniqueConstraint(
                fields=['symbol', 'holder_type'],
                name='fi_ownership_symbol_type_unique',
            ),
        ]

    def __str__(self):
        return f'{self.symbol.symbol} {self.holder_type}'


class NewsItem(models.Model):
    """News item for a symbol (from yfinance get_news)."""
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='news_items', db_index=True)
    title = models.CharField(max_length=500)
    link = models.URLField(max_length=1000, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    publisher = models.CharField(max_length=200, blank=True)
    summary = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-published_at']
        verbose_name = 'News item'
        verbose_name_plural = 'News items'

    def __str__(self):
        return f'{self.symbol.symbol} {self.title[:50]}'


class DividendCalendar(models.Model):
    """Tracks dividend calendar events: declaration, ex-date, record date, payment date."""
    STATUS_DECLARED = 'declared'
    STATUS_PAID = 'paid'
    STATUS_ESTIMATED = 'estimated'
    STATUS_CHOICES = [
        (STATUS_DECLARED, 'Declared'),
        (STATUS_PAID, 'Paid'),
        (STATUS_ESTIMATED, 'Estimated'),
    ]

    CATEGORY_REGULAR = 'regular'
    CATEGORY_SPECIAL = 'special'
    CATEGORY_FOREIGN = 'foreign'
    CATEGORY_CHOICES = [
        (CATEGORY_REGULAR, 'Regular'),
        (CATEGORY_SPECIAL, 'Special'),
        (CATEGORY_FOREIGN, 'Foreign'),
    ]

    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='dividend_calendar', db_index=True)
    declaration_date = models.DateField(null=True, blank=True)
    ex_dividend_date = models.DateField(null=True, blank=True, db_index=True)
    record_date = models.DateField(null=True, blank=True)
    payment_date = models.DateField(null=True, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    currency = models.CharField(max_length=10, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DECLARED, db_index=True)
    dividend_category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_REGULAR, db_index=True,
        help_text='regular=budgeted, special=not budgeted (only once declared), foreign=budgeted (international)',
    )
    source = models.CharField(max_length=50, blank=True, help_text='e.g. yfinance, manual')
    tm1_adjustment_written = models.BooleanField(default=False, help_text='Whether the TM1 budget adjustment has been applied')
    tm1_adjustment_value = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True,
        help_text='The adjustment value that was written to TM1',
    )
    tm1_written_at = models.DateTimeField(null=True, blank=True, help_text='When the TM1 adjustment was written')
    tm1_target_month = models.CharField(
        max_length=3, blank=True, default='',
        help_text='Resolved TM1 month for the adjustment (e.g. Apr). Set by TM1 probe or payment_date.',
    )
    tm1_verified = models.BooleanField(default=False, help_text='Whether TM1 value was verified after writing')
    tm1_verified_at = models.DateTimeField(null=True, blank=True, help_text='When TM1 was last verified')
    last_checked_at = models.DateTimeField(null=True, blank=True, help_text='When yfinance was last checked for this entry')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-ex_dividend_date', '-declaration_date']
        verbose_name = 'Dividend calendar'
        verbose_name_plural = 'Dividend calendar entries'
        constraints = [
            models.UniqueConstraint(
                fields=['symbol', 'ex_dividend_date', 'dividend_category'],
                name='fi_divcal_symbol_exdate_cat_unique',
            ),
        ]

    def __str__(self):
        return f'{self.symbol.symbol} ex:{self.ex_dividend_date} {self.amount} ({self.status})'


class WatchlistTablePreference(models.Model):
    """Stored column visibility (and other table preferences) for the financial investments watchlist."""
    key = models.CharField(max_length=64, unique=True, db_index=True, default='default')
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Watchlist table preference'
        verbose_name_plural = 'Watchlist table preferences'

    def __str__(self):
        return f'Watchlist preference {self.key}'
