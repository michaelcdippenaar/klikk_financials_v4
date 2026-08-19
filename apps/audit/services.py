"""
Runner for the year-end audit registry.

* ``validate_sql``   — read-only guard (single SELECT/WITH, no write keywords,
                       no semicolons) + EXPLAIN against the live DB.
* ``run_check``      — execute one check for a FY and tenant, classify PASS /
                       WARN / FAIL / ERROR, return a result dict.
* ``run_audit``      — execute every active check (or one), persist a
                       ``audit.check_runs`` row + ``audit.check_results`` rows.

All check SQL runs inside a READ ONLY transaction with a statement timeout, so
even a check that slipped past the keyword guard cannot mutate anything.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from decimal import Decimal
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from .models import AuditCheck, AuditCheckResult, AuditCheckRun

KLIKK_TENANT_ID = '41ebfa0e-012e-4ff1-82ba-a9a7585c536c'
SEVERITY_VALUES = ('critical', 'high', 'medium', 'low')
EXPECTED_VALUES = ('zero_rows', 'list', 'value')
SAMPLE_LIMIT = 50
STATEMENT_TIMEOUT_MS = 180_000

PARAM_RE = re.compile(r'(?<!:):(fy_start|fy_end|tenant_id)\b')
FORBIDDEN_RE = re.compile(
    r'\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|vacuum|'
    r'call|merge|refresh|reindex|cluster|lock|comment|security|set|reset|listen|notify|'
    r'pg_sleep|pg_read_file|pg_terminate_backend|pg_cancel_backend|dblink|lo_import|lo_export)\b',
    re.IGNORECASE,
)


class SqlGuardError(ValueError):
    """Raised when a check's SQL is not an acceptable read-only SELECT."""


# --------------------------------------------------------------------------- #
# Guard / binding
# --------------------------------------------------------------------------- #
def _strip_literals_and_comments(sql: str) -> str:
    """Remove string literals and comments so keyword scanning only sees code."""
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)        # 'literal' (with '' escapes)
    sql = re.sub(r'--[^\n]*', ' ', sql)                # -- comments
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.S)   # /* comments */
    return sql


def validate_sql(sql_text: str, *, explain: bool = True) -> None:
    """Reject anything that is not a single read-only SELECT. Raises SqlGuardError."""
    if not sql_text or not sql_text.strip():
        raise SqlGuardError('sql_text is empty')
    stripped = sql_text.strip().rstrip(';').strip()
    code_only = _strip_literals_and_comments(stripped)
    if ';' in code_only:
        raise SqlGuardError('multiple statements are not allowed (found ";" outside a string literal)')
    first = code_only.lstrip().split(None, 1)[0].lower() if code_only.strip() else ''
    if first not in ('select', 'with'):
        raise SqlGuardError('sql_text must start with SELECT or WITH')
    m = FORBIDDEN_RE.search(code_only)
    if m:
        raise SqlGuardError(f'forbidden keyword in sql_text: {m.group(0).upper()}')
    if explain:
        bound, params = bind_sql(stripped, fy=fiscal_year_for_today(), tenant_id=KLIKK_TENANT_ID)
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute('SET LOCAL transaction_read_only = on')
                    cur.execute(f'EXPLAIN {bound}', params)
        except Exception as exc:  # noqa: BLE001 — surface DB error text to caller
            raise SqlGuardError(f'EXPLAIN failed: {exc}') from exc


def fy_bounds(fy: int, fiscal_year_start_month: int = 7) -> tuple[dt.date, dt.date]:
    """FY N = the year in which the FY ends. Klikk: 1 Jul (N-1) .. 30 Jun N."""
    m = int(fiscal_year_start_month or 7)
    start = dt.date(fy, 1, 1) if m == 1 else dt.date(fy - 1, m, 1)
    end_exclusive = dt.date(start.year + 1, start.month, 1)
    return start, end_exclusive - dt.timedelta(days=1)


def fiscal_year_for_today(fiscal_year_start_month: int = 7) -> int:
    today = dt.date.today()
    return today.year + 1 if today.month >= fiscal_year_start_month and fiscal_year_start_month != 1 else today.year


def tenant_fy_start_month(tenant_id: str) -> int:
    with connection.cursor() as cur:
        cur.execute('SELECT fiscal_year_start_month FROM xero_core_xerotenant WHERE tenant_id = %s', [tenant_id])
        row = cur.fetchone()
    return int(row[0]) if row and row[0] else 7


def bind_sql(sql_text: str, *, fy: int, tenant_id: str, fy_start_month: int | None = None) -> tuple[str, dict]:
    """Turn ``:fy_start`` style placeholders into psycopg2 named params."""
    if fy_start_month is None:
        fy_start_month = 7
    fy_start, fy_end = fy_bounds(fy, fy_start_month)
    escaped = sql_text.replace('%', '%%')
    bound = PARAM_RE.sub(lambda m: f'%({m.group(1)})s', escaped)
    return bound, {'fy_start': fy_start, 'fy_end': fy_end, 'tenant_id': tenant_id}


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return '<bytes>'
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _classify(check: AuditCheck, rows: list[dict], row_count: int) -> tuple[str, str]:
    """Return (status, notes) from the rows of a check."""
    sev = (check.severity or 'medium').lower()
    if check.expected == 'value':
        if not rows:
            return 'ERROR', 'value check returned no rows'
        if 'ok' not in rows[0]:
            return 'ERROR', "value check must return a boolean column named 'ok'"
        bad = [r for r in rows if not r.get('ok')]
        if not bad:
            return 'PASS', ''
        return ('FAIL' if sev in ('critical', 'high') else 'WARN'), f'{len(bad)} of {len(rows)} value row(s) not ok'
    if row_count == 0:
        return 'PASS', ''
    if check.expected == 'zero_rows':
        return ('FAIL' if sev in ('critical', 'high') else 'WARN'), f'{row_count} row(s) where 0 expected'
    # 'list' — findings for human review
    return ('FAIL' if sev == 'critical' else 'WARN'), f'{row_count} row(s) listed for review'


def run_check(check: AuditCheck, *, fy: int, tenant_id: str = KLIKK_TENANT_ID, fy_start_month: int | None = None) -> dict:
    """Execute one check read-only and classify it. Never raises for SQL errors (→ ERROR)."""
    started = time.perf_counter()
    result = {
        'code': check.code,
        'title': check.title,
        'category': check.category,
        'severity': check.severity,
        'expected': check.expected,
        'owner_action': check.owner_action,
        'status': 'ERROR',
        'row_count': None,
        'sample_rows': [],
        'duration_ms': None,
        'notes': '',
    }
    try:
        validate_sql(check.sql_text, explain=False)
        bound, params = bind_sql(check.sql_text, fy=fy, tenant_id=tenant_id, fy_start_month=fy_start_month)
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute('SET LOCAL transaction_read_only = on')
                cur.execute(f'SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}')
                cur.execute(bound, params)
                cols = [c[0] for c in cur.description] if cur.description else []
                row_count = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
                sample = cur.fetchmany(SAMPLE_LIMIT)
        rows = [dict(zip(cols, map(_jsonable, r))) for r in sample]
        status, notes = _classify(check, rows, row_count)
        result.update(status=status, row_count=row_count, sample_rows=rows, notes=notes)
    except SqlGuardError as exc:
        result.update(status='ERROR', notes=f'guard: {exc}')
    except Exception as exc:  # noqa: BLE001 — one bad check must not stop the run
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        result.update(status='ERROR', notes=f'{exc.__class__.__name__}: {msg[:500]}')
    result['duration_ms'] = int((time.perf_counter() - started) * 1000)
    return result


def run_audit(*, fy: int, tenant_id: str = KLIKK_TENANT_ID, codes: list[str] | None = None,
              triggered_by: str = 'cli', include_inactive: bool = False) -> dict:
    """Run active checks (or the given codes), persist, and return the summary + results."""
    fy_start_month = tenant_fy_start_month(tenant_id)
    qs = AuditCheck.objects.all().order_by('code')
    if codes:
        qs = qs.filter(code__in=[c.upper() for c in codes])
    elif not include_inactive:
        qs = qs.filter(active=True)
    checks = list(qs)
    run = AuditCheckRun.objects.create(fy=fy, tenant_id=tenant_id, started_at=timezone.now(), triggered_by=triggered_by)

    results = []
    for check in checks:
        res = run_check(check, fy=fy, tenant_id=tenant_id, fy_start_month=fy_start_month)
        AuditCheckResult.objects.create(
            run=run, code=check.code, status=res['status'], row_count=res['row_count'],
            sample_rows=res['sample_rows'], duration_ms=res['duration_ms'], notes=res['notes'],
        )
        results.append(res)

    fy_start, fy_end = fy_bounds(fy, fy_start_month)
    counts = {s: sum(1 for r in results if r['status'] == s) for s in ('PASS', 'WARN', 'FAIL', 'ERROR')}
    summary = {
        'fy': fy, 'fy_start': fy_start.isoformat(), 'fy_end': fy_end.isoformat(), 'tenant_id': tenant_id,
        'checks_run': len(results), 'counts': counts,
        'by_category': _by_category(results),
        'findings': [
            {'code': r['code'], 'title': r['title'], 'status': r['status'], 'severity': r['severity'],
             'row_count': r['row_count'], 'owner_action': r['owner_action'], 'notes': r['notes']}
            for r in results if r['status'] in ('FAIL', 'WARN', 'ERROR')
        ],
    }
    run.finished_at = timezone.now()
    run.summary = summary
    run.save(update_fields=['finished_at', 'summary'])
    summary['run_id'] = run.run_id
    summary['started_at'] = run.started_at.isoformat()
    summary['finished_at'] = run.finished_at.isoformat()
    return {'summary': summary, 'results': results}


def _by_category(results: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in results:
        bucket = out.setdefault(r['category'], {'PASS': 0, 'WARN': 0, 'FAIL': 0, 'ERROR': 0})
        bucket[r['status']] = bucket.get(r['status'], 0) + 1
    return out


def check_to_dict(check: AuditCheck, *, include_sql: bool = False) -> dict:
    d = {
        'code': check.code, 'title': check.title, 'category': check.category, 'severity': check.severity,
        'description': check.description, 'rationale': check.rationale, 'expected': check.expected,
        'owner_action': check.owner_action, 'active': check.active, 'source': check.source,
        'created_at': check.created_at.isoformat() if check.created_at else None,
    }
    if include_sql:
        d['sql_text'] = check.sql_text
    return d


def history_for(code: str, *, limit: int = 20, fy: int | None = None) -> list[dict]:
    qs = AuditCheckResult.objects.filter(code=code.upper()).select_related('run').order_by('-id')
    if fy:
        qs = qs.filter(run__fy=fy)
    out = []
    for r in qs[:limit]:
        out.append({
            'run_id': r.run_id, 'fy': r.run.fy, 'tenant_id': r.run.tenant_id,
            'started_at': r.run.started_at.isoformat() if r.run.started_at else None,
            'triggered_by': r.run.triggered_by, 'status': r.status, 'row_count': r.row_count,
            'duration_ms': r.duration_ms, 'notes': r.notes, 'sample_rows': r.sample_rows or [],
        })
    return out
