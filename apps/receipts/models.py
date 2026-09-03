"""
Review state for the WhatsApp Slippies receipt register (``whatsapp.klikk_slips``).

``klikk_slips`` itself is maintained by an external sync and is read here only
through raw SQL (see ``services``). These two tables are the only thing this app
writes; ``sha256`` is a loose key into ``klikk_slips`` — no FK, because the
register is not a Django model and must never be altered.
"""
from django.db import models


DECISION_CHOICES = (
    ('', 'Undecided'),
    ('CAPTURE', 'Capture in Xero'),
    ('MEAL_SKIP', 'Meal — skip'),
    ('PERSONAL', 'Personal'),
    ('DUPLICATE', 'Duplicate'),
    ('ALREADY_IN_XERO', 'Already in Xero'),
)
DECISION_VALUES = tuple(value for value, _ in DECISION_CHOICES)


class SlipReview(models.Model):
    sha256 = models.CharField(max_length=64, primary_key=True)
    to_process = models.BooleanField(default=False)
    decision = models.CharField(max_length=20, blank=True, default='', choices=DECISION_CHOICES)
    note = models.TextField(blank=True, default='')
    # Archive = "dealt with, clear it from the working list" — soft and reversible, never a
    # delete (the register itself is untouched; this only hides the row from the default
    # list filter). Indexed because EVERY list/export call filters on it (services.build_filters
    # excludes archived rows unless asked otherwise).
    archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.CharField(max_length=150, blank=True, default='')
    updated_by = models.CharField(max_length=150, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Slip review'

    def __str__(self):
        return f'{self.sha256[:12]} {self.decision or "-"}'


class SlipComment(models.Model):
    """A comment on one receipt. Threaded ONE level deep.

    ``parent`` is a self-FK rather than a thread-id column so a reply cannot
    reference a comment that does not exist. The view flattens: replying to a
    reply re-parents onto that reply's root, so the tree is never deeper than
    parent -> replies. That is a deliberate product choice (the register is
    triaged in a narrow cell; arbitrarily deep nesting has nowhere to render)
    and it means the read side never needs recursion.

    CASCADE on delete: nothing in the app deletes comments today, but if a
    parent ever goes, orphan replies would render as ghost top-level comments.
    """

    sha256 = models.CharField(max_length=64, db_index=True)
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.CASCADE, related_name='replies',
    )
    text = models.TextField()
    author = models.CharField(max_length=150, blank=True, default='')
    # Indexed because the live-comment feed pages this table by created_at on
    # every poll, from every open console tab.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Slip comment'

    def __str__(self):
        return f'{self.sha256[:12]} by {self.author or "?"}'
