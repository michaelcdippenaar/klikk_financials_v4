"""
The append-only activity trail for the audit surface — "who did what".

Scope, as chosen: every WRITE on the audit surface (findings, receipts,
comments, attachments, links, bulk actions), plus READS by auditor accounts of
finding detail and of slip / attachment files. Standard users' reads are not
logged — the point is accountability for the external parties who have been
given access to the books, not surveillance of the team.

Append-only means append-only: nothing in this app updates or deletes a row,
and the admin is registered read-only. A trail an operator can quietly amend
is not a trail.

``changes`` holds {field: {from, to}} for updates, or {'ids': [...], 'count': n}
for bulk actions — one event per bulk ACTION, never one per affected object,
or a 500-row bulk archive would bury everything else in the table.
"""
from django.conf import settings
from django.db import models

# --- action slugs -----------------------------------------------------------
# Kept as a flat tuple rather than a TextChoices enum because the set grows
# with the app and a migration per new verb would be pure friction. Validated
# by the recorder only in the sense that anything unknown still records.
FINDING_CREATED = 'finding.created'
FINDING_STATUS_CHANGED = 'finding.status_changed'
FINDING_OWNER_CHANGED = 'finding.owner_changed'
FINDING_DUE_CHANGED = 'finding.due_changed'
FINDING_AMOUNT_CHANGED = 'finding.amount_changed'
FINDING_SEVERITY_CHANGED = 'finding.severity_changed'
FINDING_CATEGORY_CHANGED = 'finding.category_changed'
FINDING_TITLE_CHANGED = 'finding.title_changed'
FINDING_UPDATED = 'finding.updated'
FINDING_BULK_STATUS = 'finding.bulk_status'
FINDING_BULK_OWNER = 'finding.bulk_owner'
FINDING_BULK_DUE = 'finding.bulk_due'
FINDING_BULK_COMMENT = 'finding.bulk_comment'
FINDING_VIEWED = 'finding.viewed'

RECEIPT_TO_PROCESS_SET = 'receipt.to_process_set'
RECEIPT_ARCHIVED = 'receipt.archived'
RECEIPT_RESTORED = 'receipt.restored'
RECEIPT_REVIEW_SAVED = 'receipt.review_saved'
RECEIPT_BULK_REVIEW = 'receipt.bulk_review'
RECEIPT_BULK_COMMENT = 'receipt.bulk_comment'

COMMENT_POSTED = 'comment.posted'
# Cube-comment threads: a reply on a comment in the register (app.cube_comments),
# and — auditors only, like every other read here — a view of one of those threads.
CUBE_COMMENT_REPLIED = 'cube_comment.replied'
CUBE_COMMENT_VIEWED = 'cube_comment.viewed'
ATTACHMENT_UPLOADED = 'attachment.uploaded'
ATTACHMENT_DELETED = 'attachment.deleted'
ATTACHMENT_VIEWED = 'attachment.viewed'
LINK_ADDED = 'link.added'
LINK_REMOVED = 'link.removed'
SLIP_VIEWED = 'slip.viewed'

ACTIONS = (
    FINDING_CREATED, FINDING_STATUS_CHANGED, FINDING_OWNER_CHANGED, FINDING_DUE_CHANGED,
    FINDING_AMOUNT_CHANGED, FINDING_SEVERITY_CHANGED, FINDING_CATEGORY_CHANGED,
    FINDING_TITLE_CHANGED, FINDING_UPDATED, FINDING_BULK_STATUS, FINDING_BULK_OWNER,
    FINDING_BULK_DUE, FINDING_BULK_COMMENT, FINDING_VIEWED,
    RECEIPT_TO_PROCESS_SET, RECEIPT_ARCHIVED, RECEIPT_RESTORED, RECEIPT_REVIEW_SAVED,
    RECEIPT_BULK_REVIEW, RECEIPT_BULK_COMMENT,
    COMMENT_POSTED, CUBE_COMMENT_REPLIED, CUBE_COMMENT_VIEWED,
    ATTACHMENT_UPLOADED, ATTACHMENT_DELETED, ATTACHMENT_VIEWED,
    LINK_ADDED, LINK_REMOVED, SLIP_VIEWED,
)

TARGET_KINDS = (
    ('finding', 'Finding'),
    ('receipt', 'Receipt'),
    ('comment', 'Comment'),
    ('cube_comment', 'Cube comment'),
    ('attachment', 'Attachment'),
    ('link', 'Link'),
)

SOURCES = (
    ('console', 'Console'),
    ('mcp', 'MCP / service token'),
    ('bulk', 'Bulk action'),
    ('system', 'System'),
)


class ActivityEvent(models.Model):
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Both: the string survives the user row being renamed or removed, the FK
    # makes "everything this account did" a join rather than a text match.
    actor = models.CharField(max_length=150, blank=True, default='', db_index=True)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='activity_events',
    )
    actor_role = models.CharField(max_length=16, blank=True, default='')

    action = models.CharField(max_length=48, db_index=True)
    target_kind = models.CharField(max_length=16, choices=TARGET_KINDS, blank=True, default='')
    target_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    target_ref = models.CharField(max_length=300, blank=True, default='')

    changes = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=SOURCES, default='console')

    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default='')
    request_id = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(fields=['target_kind', 'target_id', '-occurred_at']),
            models.Index(fields=['actor', '-occurred_at']),
        ]
        verbose_name = 'Activity event'

    def __str__(self):
        return f'{self.occurred_at:%Y-%m-%d %H:%M} {self.actor or "?"} {self.action}'
