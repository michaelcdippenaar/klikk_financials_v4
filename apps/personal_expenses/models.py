from django.db import models

from apps.investec.models import InvestecBankTransaction


class Category(models.Model):
    """A spending/income category, e.g. (personal, 'Groceries').

    `type` buckets the category so the personal-expenses report can filter to
    `type='personal'` while business / income / transfer transactions are kept
    out of the personal view.
    """

    TYPE_PERSONAL = 'personal'
    TYPE_BUSINESS = 'business'
    TYPE_INCOME = 'income'
    TYPE_TRANSFER = 'transfer'
    TYPE_REVIEW = 'review'
    TYPE_CHOICES = [
        (TYPE_PERSONAL, 'Personal'),
        (TYPE_BUSINESS, 'Business'),
        (TYPE_INCOME, 'Income'),
        (TYPE_TRANSFER, 'Transfer'),
        (TYPE_REVIEW, 'Review'),
    ]

    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_PERSONAL, db_index=True)
    name = models.CharField(max_length=100)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['type', 'name']
        verbose_name = 'Category'
        verbose_name_plural = 'Categories'
        constraints = [
            models.UniqueConstraint(fields=['type', 'name'], name='pe_category_type_name_unique'),
        ]

    def __str__(self):
        return f'{self.type} / {self.name}'


class ClassificationRule(models.Model):
    """A tag -> category rule.

    Matches a transaction when `tag` (stored UPPERCASE) is a substring of the
    upper-cased description AND (`transaction_type` == txn.transaction_type OR
    `transaction_type` == '' which means "any"). Longest matching tag wins.
    """

    tag = models.CharField(max_length=120, db_index=True, help_text="Stored UPPERCASE; substring-matched against the description.")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='rules')
    transaction_type = models.CharField(
        max_length=40, blank=True, db_index=True,
        help_text="Investec transaction_type to gate on (CardPurchases, OnlineBankingPayments, ...). Blank = any.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    priority = models.IntegerField(default=0, help_text="Tie-breaker when two tags are the same length; higher wins.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-priority', 'tag']
        verbose_name = 'Classification Rule'
        verbose_name_plural = 'Classification Rules'
        constraints = [
            models.UniqueConstraint(fields=['tag', 'transaction_type'], name='pe_rule_tag_tt_unique'),
        ]

    def __str__(self):
        return f'{self.tag} [{self.transaction_type or "any"}] -> {self.category}'


class TransactionClassification(models.Model):
    """The single current category assigned to an InvestecBankTransaction.

    `is_manual=True` marks a human override that the auto-classifier must never
    clobber on a re-run.
    """

    SOURCE_AUTO = 'auto'
    SOURCE_MANUAL = 'manual'
    SOURCE_CHOICES = [(SOURCE_AUTO, 'Auto'), (SOURCE_MANUAL, 'Manual')]

    transaction = models.OneToOneField(
        InvestecBankTransaction,
        on_delete=models.CASCADE,
        related_name='classification',
        db_index=True,
    )
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='classifications')
    rule = models.ForeignKey(
        ClassificationRule, on_delete=models.SET_NULL, null=True, blank=True, related_name='classifications',
    )
    is_manual = models.BooleanField(default=False, db_index=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_AUTO)
    matched_tag = models.CharField(max_length=120, blank=True, help_text="The rule tag that matched (provenance / debugging).")

    classified_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Transaction Classification'
        verbose_name_plural = 'Transaction Classifications'
        indexes = [models.Index(fields=['category', 'is_manual'])]

    def __str__(self):
        return f'txn={self.transaction_id} -> {self.category} ({self.source})'
