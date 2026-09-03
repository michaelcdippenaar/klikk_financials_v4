"""
Year-end audit registry — Postgres schema ``audit``.

Tables are created by a raw-SQL migration (0001) so that the existing
``audit.xero_writes`` table (written by other tooling) is never touched by
Django. The models below are therefore ``managed = False`` and use the
``schema"."table`` quoting trick so the ORM addresses ``"audit"."checks"``.

Check SQL is parameterised with ``:fy_start``, ``:fy_end`` and ``:tenant_id``
and must be a single read-only SELECT (see ``services.validate_sql``).
"""
import posixpath
import re

from django.db import models


SEVERITIES = ('critical', 'high', 'medium', 'low')
EXPECTED = ('zero_rows', 'list', 'value')
STATUSES = ('PASS', 'WARN', 'FAIL', 'ERROR')


class AuditCheck(models.Model):
    code = models.CharField(primary_key=True, max_length=16)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=32)
    severity = models.CharField(max_length=16)
    description = models.TextField()
    rationale = models.TextField(blank=True, default='')
    sql_text = models.TextField()
    expected = models.CharField(max_length=16)
    owner_action = models.TextField(blank=True, default='')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    source = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        managed = False
        db_table = 'audit"."checks'
        ordering = ['code']

    def __str__(self):
        return f'{self.code} {self.title}'


class AuditCheckRun(models.Model):
    run_id = models.BigAutoField(primary_key=True)
    fy = models.IntegerField()
    tenant_id = models.CharField(max_length=64)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.CharField(max_length=64, blank=True, default='')
    summary = models.JSONField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'audit"."check_runs'
        ordering = ['-run_id']


class AuditCheckResult(models.Model):
    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(AuditCheckRun, on_delete=models.DO_NOTHING, db_column='run_id', related_name='results')
    code = models.CharField(max_length=16)
    status = models.CharField(max_length=8)
    row_count = models.IntegerField(null=True, blank=True)
    sample_rows = models.JSONField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        managed = False
        db_table = 'audit"."check_results'
        ordering = ['-id']


# --------------------------------------------------------------------------- #
# Audit findings register
#
# Unlike the registry above (managed=False over the raw Postgres schema
# ``audit``), the three models below ARE ordinary Django-managed tables in the
# default/public schema (``audit_auditfinding`` etc.), created by migration
# 0002. They hold the human-curated findings workflow (register / comment /
# attach evidence / link to source entities), not the deterministic check
# results.
#
# !! MIGRATION HAZARD -- READ BEFORE ANY AlterField ON THESE MODELS !!
# The Postgres view audit.v_finding_graph (migration 0004) SELECTs these
# columns:
#     AuditFindingLink.kind / .ref / .label
#     AuditFindingAttachment.original_name
#     AuditFinding.check_code (and .fy)
# Postgres will not let you ALTER the type or length of a column a view depends
# on -- the migration fails with 'cannot alter type of a column used by a view'.
# Any future AlterField on those columns MUST drop and recreate the view in the
# SAME migration, or the deploy breaks halfway. Adding NEW columns is safe; it
# is only altering the ones listed above that is blocked.
# --------------------------------------------------------------------------- #

FINDING_SEVERITIES = ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO')
FINDING_STATUSES = ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'ACCEPTED', 'WITHDRAWN')
LINK_KINDS = ('slip', 'xero_document', 'bank_transaction', 'journal', 'invoice', 'asana')


class AuditFinding(models.Model):
    fy = models.IntegerField(db_index=True)
    ref = models.CharField(max_length=20)  # 'FY26-012'
    title = models.CharField(max_length=300)
    severity = models.CharField(max_length=16, default='MEDIUM')  # FINDING_SEVERITIES
    status = models.CharField(max_length=16, default='OPEN', db_index=True)  # FINDING_STATUSES
    category = models.CharField(max_length=32, blank=True, default='OTHER')
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, default='ZAR')
    description = models.TextField(blank=True, default='')
    evidence = models.JSONField(default=list, blank=True)  # list of {type, ref, note}
    owner = models.CharField(max_length=150, blank=True, default='')
    due_date = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=200)  # required on create
    check_code = models.CharField(max_length=16, blank=True, default='', db_index=True)
    asana_gid = models.CharField(max_length=64, blank=True, default='')
    # Saved pivot: the SAME {name, spec, query} shape the Excel add-in and
    # app.cube_views already speak, so no second pivot-spec format exists.
    cube_view = models.JSONField(null=True, blank=True)
    cube_note = models.TextField(blank=True, default='')
    created_by = models.CharField(max_length=150, blank=True, default='')
    updated_by = models.CharField(max_length=150, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-fy', 'ref']
        constraints = [
            models.UniqueConstraint(fields=['fy', 'ref'], name='uniq_auditfinding_ref_per_fy'),
        ]
        indexes = [
            models.Index(fields=['fy', 'status']),
            models.Index(fields=['fy', 'severity']),
        ]

    def __str__(self):
        return f'{self.ref} {self.title}'


class AuditFindingComment(models.Model):
    """A comment on one finding. Threaded ONE level deep — see SlipComment for
    the reasoning; the two comment surfaces deliberately behave identically so
    the console can render both with the same component."""

    finding = models.ForeignKey(AuditFinding, on_delete=models.CASCADE, related_name='comments')
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



class CommentWebhookDelivery(models.Model):
    """One row per outbound comment-webhook ATTEMPT — success or failure.

    This is the "log of who sent it": it answers, after the fact, which comment
    was pushed where, by whom, and what the far end said. Written even when the
    POST fails or the far end 500s, because a webhook that silently stopped
    delivering is exactly the failure this table exists to surface.

    Append-only by convention (no update/delete path exists in the app, and the
    admin registers it read-only). ``response_snippet`` is capped so a chatty
    or hostile endpoint cannot use this table as free storage.
    """

    KIND_CHOICES = (('receipt', 'Receipt'), ('finding', 'Finding'))
    SNIPPET_MAX = 500

    comment_kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    comment_id = models.BigIntegerField(db_index=True)
    author = models.CharField(max_length=150, blank=True, default='')
    target_url = models.URLField(max_length=500)
    status_code = models.IntegerField(null=True, blank=True)  # None when the POST never completed
    error = models.TextField(blank=True, default='')
    response_snippet = models.TextField(blank=True, default='')
    attempted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-attempted_at']
        verbose_name = 'Comment webhook delivery'
        verbose_name_plural = 'Comment webhook deliveries'

    def __str__(self):
        outcome = self.status_code if self.status_code is not None else (self.error or 'no response')
        return f'{self.comment_kind}#{self.comment_id} -> {outcome}'


_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f\\/]+')
_WHITESPACE = re.compile(r'\s+')


def sanitise_attachment_filename(filename, budget=None):
    """Reduce an uploaded name to a safe single path segment.

    Strips directory separators and control characters (including NUL), collapses
    whitespace, and refuses ``.``/``..``. The ORIGINAL name is still preserved
    verbatim in ``original_name`` — this only governs what lands on disk.
    ``budget``, when given, caps the result length, shortening the STEM so the
    extension (which the signed viewer keys its content type off) survives.
    """
    name = _WHITESPACE.sub(' ', _UNSAFE_FILENAME.sub('', filename or '')).strip()
    name = posixpath.basename(name)
    if not name or name in {'.', '..'}:
        name = 'attachment'
    if budget and len(name) > budget:
        stem, dot, ext = name.rpartition('.')
        if dot and 0 < len(ext) <= 12:
            name = (stem[:max(budget - len(ext) - 1, 1)] or 'attachment') + '.' + ext
        else:
            name = name[:budget]
        name = name[:budget]
    return name


def finding_attachment_path(instance, filename):
    """``audit_findings/<fy>/<finding_id>/<filename>``.

    Grouping by FY then finding keeps a year's evidence together on disk, which
    is what an external auditor asks for ("give me the FY2026 evidence folder"),
    and keeps any single directory small. ``fy``/``finding_id`` are read off the
    already-saved parent finding; the ``0`` fallbacks only apply if an
    attachment is ever built without one, which the upload endpoint prevents.

    The stem is truncated so the whole path fits FileField's ``max_length``:
    without this a legitimately long filename raises a DataError at save time
    instead of just uploading. ``original_name`` keeps the untruncated name.
    """
    finding = getattr(instance, 'finding', None)
    fy = getattr(finding, 'fy', None) or 0
    finding_id = getattr(instance, 'finding_id', None) or 0
    prefix = posixpath.join('audit_findings', str(fy), str(finding_id))
    max_length = instance._meta.get_field('file').max_length or 100
    # leave headroom for the storage backend's collision suffix ("_a1b2c3d")
    budget = max_length - len(prefix) - len('/') - 8
    return posixpath.join(prefix, sanitise_attachment_filename(filename, budget=budget))


class AuditFindingAttachment(models.Model):
    finding = models.ForeignKey(AuditFinding, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to=finding_attachment_path)
    original_name = models.CharField(max_length=255, blank=True, default='')
    content_type = models.CharField(max_length=120, blank=True, default='')
    size = models.BigIntegerField(default=0)
    # A supporting document almost always needs a line of context to be useful a
    # year later -- 'Aurras statement 31 Oct, shows R0 due' says something the
    # filename never will. Catalog-only, so adding it is zero-downtime.
    note = models.TextField(blank=True, default='')
    uploaded_by = models.CharField(max_length=150, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class AuditFindingLink(models.Model):
    """A typed pointer from a finding to a source entity elsewhere in the stack.

    ``ref`` is deliberately a plain string rather than a real FK: the targets live
    in tables this app does not own (``whatsapp.klikk_slips``, the Xero document /
    journal mirrors, ``investec_investecbanktransaction``) and in Asana, which is
    not in this database at all. A dangling ref is NORMAL — a slip can be purged
    while the finding that cited it must survive — so resolution degrades to
    ``found: false`` instead of erroring, and no FK is allowed to cascade a
    finding away.

    The ``(kind, ref)`` index is the REVERSE lookup — "which findings cite this
    slip" — which is what the graph traversal walks; it is not optional.
    """

    finding = models.ForeignKey(AuditFinding, on_delete=models.CASCADE, related_name='links')
    kind = models.CharField(max_length=24)  # LINK_KINDS
    # slip sha256 / document id / bank txn id / journal_number / invoice_number / asana gid
    ref = models.CharField(max_length=128)
    label = models.CharField(max_length=300, blank=True, default='')
    added_by = models.CharField(max_length=150, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['kind', 'id']
        constraints = [
            models.UniqueConstraint(fields=['finding', 'kind', 'ref'], name='uniq_findinglink'),
        ]
        indexes = [
            models.Index(fields=['kind', 'ref']),  # reverse: which findings cite this slip
        ]

    def __str__(self):
        return f'{self.kind}:{self.ref}'
