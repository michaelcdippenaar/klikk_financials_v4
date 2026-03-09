from django.db import models


# ------------------------------------------------
# Map Share_Name to Company to Share_Code
# ------------------------------------------------

class InvestecJseShareNameMapping(models.Model):
    """Model to map Share Names from transactions to Companies and Share Codes from portfolios."""
    
    share_name = models.CharField(max_length=100, unique=True, db_index=True)  # Primary share name (required, unique)
    share_name2 = models.CharField(max_length=100, blank=True, null=True, db_index=True)  # Alternative share name 2 (optional)
    share_name3 = models.CharField(max_length=100, blank=True, null=True, db_index=True)  # Alternative share name 3 (optional)
    company = models.CharField(max_length=100, blank=True, null=True, db_index=True)  # From portfolios (optional)
    share_code = models.CharField(max_length=20, blank=True, null=True, db_index=True)  # From portfolios (optional); unique when set

    class Meta:
        ordering = ['share_name']
        verbose_name = 'Investec Jse Share Name Mapping'
        verbose_name_plural = 'Investec Jse Share Name Mappings'
        indexes = [
            models.Index(fields=['share_name']),
            models.Index(fields=['share_name2']),
            models.Index(fields=['share_name3']),
            models.Index(fields=['company']),
            models.Index(fields=['share_code']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['share_code'],
                name='investec_unique_share_code',
                condition=models.Q(share_code__isnull=False) & ~models.Q(share_code=''),
            ),
        ]
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        if self.company and self.share_code:
            return f"{self.share_name} -> {self.company} ({self.share_code})"
        elif self.company:
            return f"{self.share_name} -> {self.company}"
        else:
            return f"{self.share_name}"

# ------------------------------------------------
# Transaction Models
# ------------------------------------------------


class InvestecJseTransaction(models.Model):
    """Model to store Investec transaction data."""
    
    date = models.DateField()
    year = models.IntegerField(null=True, blank=True)
    month = models.IntegerField(null=True, blank=True)
    day = models.IntegerField(null=True, blank=True)
    account_number = models.CharField(max_length=50)
    description = models.CharField(max_length=255)
    share_name = models.CharField(max_length=100, blank=True)  # Can be empty for account-related transactions
    type = models.CharField(max_length=50)  # e.g., 'Buy', 'Sell', 'Dividend', 'Fee', 'Broker Fee', etc.
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    value = models.DecimalField(max_digits=15, decimal_places=2)
    value_per_share = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)  # Value per share in rands (only for Buy/Sell transactions)
    value_calculated = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)  # Calculated value: value_per_share * quantity (negative for Buy transactions)
    dividend_ttm = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)  # Trailing 12-month dividend sum
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-date', '-created_at']
        verbose_name = 'Investec Jse Transaction'
        verbose_name_plural = 'Investec Jse Transactions'
        indexes = [
            models.Index(fields=['year', 'month']),
            models.Index(fields=['date']),
        ]
    
    def save(self, *args, **kwargs):
        """Automatically populate year, month, day from date field."""
        if self.date:
            self.year = self.date.year
            self.month = self.date.month
            self.day = self.date.day
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.date} - {self.share_name} - {self.type} - {self.quantity}"


# ------------------------------------------------
# Portfolio Models
# ------------------------------------------------


class InvestecJsePortfolio(models.Model):
    """Model to store Investec portfolio data."""
    
    date = models.DateField()
    year = models.IntegerField(null=True, blank=True)
    month = models.IntegerField(null=True, blank=True)
    day = models.IntegerField(null=True, blank=True)
    company = models.CharField(max_length=100)
    share_code = models.CharField(max_length=20)
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    currency = models.CharField(max_length=10, default='ZAR')
    unit_cost = models.DecimalField(max_digits=15, decimal_places=4)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2)
    price = models.DecimalField(max_digits=15, decimal_places=4)
    total_value = models.DecimalField(max_digits=15, decimal_places=2)
    exchange_rate = models.DecimalField(max_digits=15, decimal_places=6, null=True, blank=True)
    move_percent = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)  # Move %
    portfolio_percent = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)  # Portfolio %
    profit_loss = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)  # Profit/Loss
    annual_income_zar = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)  # Annual Income (R)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-date', 'company']
        verbose_name = 'Investec Jse Portfolio'
        verbose_name_plural = 'Investec Jse Portfolios'
        indexes = [
            models.Index(fields=['date', 'company']),
            models.Index(fields=['share_code']),
            models.Index(fields=['year', 'month']),
        ]
    
    def save(self, *args, **kwargs):
        """Automatically populate year, month, day from date field and update share code mapping."""
        if self.date:
            self.year = self.date.year
            self.month = self.date.month
            self.day = self.date.day
        
        super().save(*args, **kwargs)
        
        # Update share code mappings when portfolio is saved (after save to avoid circular issues)
        # Update all mappings with this share_code to have the company name and share_code
        if self.share_code and self.company:
            InvestecJseShareNameMapping.objects.filter(share_code=self.share_code).update(
                company=self.company,
                share_code=self.share_code,
            )
    
    def __str__(self):
        return f"{self.date} - {self.company} ({self.share_code}) - Qty: {self.quantity}"


# ------------------------------------------------
# Share Performance Models
# ------------------------------------------------

class InvestecJseShareMonthlyPerformance(models.Model):
    """Model to store monthly share performance metrics including TTM dividends and dividend yield."""
    
    share_name = models.CharField(max_length=100, db_index=True)
    date = models.DateField()  # Month End date
    year = models.IntegerField(null=True, blank=True)
    month = models.IntegerField(null=True, blank=True)
    dividend_type = models.CharField(max_length=50, db_index=True)  # Dividend type: 'Dividend', 'Special Dividend', 'Foreign Dividend', 'Dividend Tax'
    investec_account = models.CharField(max_length=50, blank=True, null=True, db_index=True)  # Account number from transactions
    dividend_ttm = models.DecimalField(max_digits=15, decimal_places=2)  # Trailing 12-month dividend sum
    closing_price = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)  # Share price at month end
    quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)  # Quantity held at month end
    total_market_value = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)  # Total market value (Quantity × Price)
    dividend_yield = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)  # e.g., 0.0523 for 5.23% (dividend_ttm / total_market_value)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-date', 'share_name']
        verbose_name = 'Investec Jse Share Monthly Performance'
        verbose_name_plural = 'Investec Jse Share Monthly Performances'
        unique_together = ('share_name', 'date', 'dividend_type')
        indexes = [
            models.Index(fields=['share_name', 'date']),
            models.Index(fields=['date']),
            models.Index(fields=['year', 'month']),
            models.Index(fields=['dividend_type']),
            models.Index(fields=['investec_account']),
        ]
    
    def save(self, *args, **kwargs):
        """Automatically populate year and month from date field."""
        if self.date:
            self.year = self.date.year
            self.month = self.date.month
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.share_name} - {self.date} - TTM: {self.dividend_ttm}"


# ------------------------------------------------
# Investec Private Banking (Bank Account) Models
# ------------------------------------------------
# Data from Investec SA PB Account Information API (openapi.investec.com).
# Separate from JSE securities models above.


class InvestecBankAccount(models.Model):
    """Investec Private Bank account (current/checking). Synced from GET /za/pb/v1/accounts."""

    account_id = models.CharField(max_length=40, unique=True, db_index=True)
    account_number = models.CharField(max_length=40)
    account_name = models.CharField(max_length=70, blank=True)
    reference_name = models.CharField(max_length=70, blank=True)
    product_name = models.CharField(max_length=70, blank=True)
    kyc_compliant = models.BooleanField(default=False)
    profile_id = models.CharField(max_length=70, blank=True)
    profile_name = models.CharField(max_length=70, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['account_number']
        verbose_name = 'Investec Bank Account'
        verbose_name_plural = 'Investec Bank Accounts'

    def __str__(self):
        return f"{self.account_number} – {self.account_name or self.reference_name or 'Investec'}"


class InvestecBankTransaction(models.Model):
    """Single transaction on an Investec Private Bank account. Synced from GET /za/pb/v1/accounts/{id}/transactions."""

    TYPE_CREDIT = 'CREDIT'
    TYPE_DEBIT = 'DEBIT'
    TYPE_CHOICES = [(TYPE_CREDIT, 'Credit'), (TYPE_DEBIT, 'Debit')]

    STATUS_POSTED = 'POSTED'
    STATUS_PENDING = 'PENDING'
    STATUS_CHOICES = [(STATUS_POSTED, 'Posted'), (STATUS_PENDING, 'Pending')]

    account = models.ForeignKey(
        InvestecBankAccount,
        on_delete=models.CASCADE,
        related_name='transactions',
        db_index=True,
    )
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    transaction_type = models.CharField(max_length=40, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    card_number = models.CharField(max_length=40, blank=True)
    posted_order = models.IntegerField(null=True, blank=True)
    posting_date = models.DateField(null=True, blank=True)
    value_date = models.DateField(null=True, blank=True)
    action_date = models.DateField(null=True, blank=True)
    transaction_date = models.DateField(null=True, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    running_balance = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    uuid = models.CharField(max_length=40, blank=True, null=True, unique=True, db_index=True)
    fallback_key = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
        help_text="Stable hash when API returns no uuid/posted_order; (transaction_date, value_date, action_date, amount, description).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-posting_date', '-posted_order']
        verbose_name = 'Investec Bank Transaction'
        verbose_name_plural = 'Investec Bank Transactions'
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'posting_date', 'posted_order'],
                name='investec_bank_txn_account_posting_order',
                condition=models.Q(posting_date__isnull=False) & models.Q(posted_order__isnull=False),
            ),
        ]
        indexes = [
            models.Index(fields=['account', 'posting_date']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.account.account_number} {self.posting_date} {self.type} {self.amount} – {self.description[:30]}"


class InvestecBankSyncLog(models.Model):
    """Single row storing last Investec bank sync time. Used for incremental updates."""

    key = models.CharField(max_length=32, unique=True, default='default')
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Investec Bank Sync Log'
        verbose_name_plural = 'Investec Bank Sync Logs'