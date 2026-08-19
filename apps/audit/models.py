"""
Year-end audit registry — Postgres schema ``audit``.

Tables are created by a raw-SQL migration (0001) so that the existing
``audit.xero_writes`` table (written by other tooling) is never touched by
Django. The models below are therefore ``managed = False`` and use the
``schema"."table`` quoting trick so the ORM addresses ``"audit"."checks"``.

Check SQL is parameterised with ``:fy_start``, ``:fy_end`` and ``:tenant_id``
and must be a single read-only SELECT (see ``services.validate_sql``).
"""
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
