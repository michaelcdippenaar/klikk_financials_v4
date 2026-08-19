"""
REST surface for the year-end audit registry — consumed by the klikk-financials
MCP server (klikk_portal/mcp/stock-market/server.mjs) and usable directly.

GET  /audit/checks/?category=BAL&include_sql=1   list_audit_checks
POST /audit/checks/                              add_audit_check (validates SQL via EXPLAIN, SELECT-only)
GET  /audit/checks/<code>/                       one check incl. sql_text
POST /audit/checks/<code>/run/   {fy}            run_audit_check
POST /audit/run/                 {fy}            run_yearend_audit
GET  /audit/history/<code>/?limit=20&fy=2026     audit_history
GET  /audit/runs/?limit=20                       recent runs

Everything except POST /checks/ is read-only on business data; the registry
itself (audit.checks / check_runs / check_results) is the only thing written.
Xero is never touched.
"""
from django.db import IntegrityError
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import AuditCheck, AuditCheckRun
from .services import (
    EXPECTED_VALUES, SEVERITY_VALUES, KLIKK_TENANT_ID, SqlGuardError, check_to_dict, fiscal_year_for_today,
    history_for, run_audit, run_check, validate_sql,
)


def _int(value, default, lo=None, hi=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _fy(request_data):
    fy = _int(request_data.get('fy'), None, 2015, 2100)
    return fy if fy else fiscal_year_for_today()


@api_view(['GET', 'POST'])
def checks_view(request):
    if request.method == 'GET':
        qs = AuditCheck.objects.all().order_by('code')
        category = (request.query_params.get('category') or '').strip().upper()
        if category:
            qs = qs.filter(category=category)
        if request.query_params.get('active') in ('1', 'true', 'True'):
            qs = qs.filter(active=True)
        # sql_text is included by default (the console's Audit Procedures page shows it);
        # pass include_sql=0 for a lighter payload (the MCP list tool does).
        include_sql = request.query_params.get('include_sql', '1') not in ('0', 'false', 'False')
        checks = [check_to_dict(c, include_sql=include_sql) for c in qs]
        categories = sorted({c['category'] for c in checks})
        # registry_ready/categories keep the contract the console (klikk_portal AuditProcedures.vue) relies on.
        return Response({'registry_ready': True, 'count': len(checks), 'categories': categories, 'checks': checks})

    # POST — add a check
    data = request.data or {}
    required = ('code', 'title', 'category', 'severity', 'description', 'sql_text', 'expected')
    missing = [k for k in required if not str(data.get(k, '')).strip()]
    if missing:
        return Response({'detail': f'missing required field(s): {", ".join(missing)}'}, status=status.HTTP_400_BAD_REQUEST)
    code = str(data['code']).strip().upper()
    severity = str(data['severity']).strip().lower()
    expected = str(data['expected']).strip().lower()
    if severity not in SEVERITY_VALUES:
        return Response({'detail': f'severity must be one of {SEVERITY_VALUES}'}, status=status.HTTP_400_BAD_REQUEST)
    if expected not in EXPECTED_VALUES:
        return Response({'detail': f'expected must be one of {EXPECTED_VALUES}'}, status=status.HTTP_400_BAD_REQUEST)
    sql_text = str(data['sql_text']).strip()
    try:
        validate_sql(sql_text, explain=True)
    except SqlGuardError as exc:
        return Response({'detail': f'sql_text rejected: {exc}'}, status=status.HTTP_400_BAD_REQUEST)
    if AuditCheck.objects.filter(code=code).exists() and not data.get('replace'):
        return Response({'detail': f'check {code} already exists (pass replace=true to update it)'},
                        status=status.HTTP_409_CONFLICT)
    try:
        obj, created = AuditCheck.objects.update_or_create(
            code=code,
            defaults={
                'title': str(data['title']).strip(), 'category': str(data['category']).strip().upper(),
                'severity': severity, 'description': str(data['description']).strip(),
                'rationale': str(data.get('rationale', '')).strip(), 'sql_text': sql_text, 'expected': expected,
                'owner_action': str(data.get('owner_action', '')).strip(), 'active': bool(data.get('active', True)),
                'source': str(data.get('source', 'mcp:add_audit_check')).strip(),
            },
        )
    except IntegrityError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    # Smoke-run it for the current FY so the caller sees it works end-to-end.
    smoke = run_check(obj, fy=_fy(data), tenant_id=KLIKK_TENANT_ID)
    return Response({'created': created, 'check': check_to_dict(obj, include_sql=True), 'smoke_run': smoke},
                    status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(['GET'])
def check_detail_view(request, code):
    try:
        check = AuditCheck.objects.get(code=code.upper())
    except AuditCheck.DoesNotExist:
        return Response({'detail': f'unknown check {code}'}, status=status.HTTP_404_NOT_FOUND)
    return Response(check_to_dict(check, include_sql=True))


@api_view(['POST'])
def run_check_view(request, code):
    try:
        check = AuditCheck.objects.get(code=code.upper())
    except AuditCheck.DoesNotExist:
        return Response({'detail': f'unknown check {code}'}, status=status.HTTP_404_NOT_FOUND)
    data = request.data or {}
    fy = _fy(data)
    tenant_id = str(data.get('tenant_id') or KLIKK_TENANT_ID)
    out = run_audit(fy=fy, tenant_id=tenant_id, codes=[check.code],
                    triggered_by=str(data.get('triggered_by') or 'api:run_audit_check'))
    result = out['results'][0] if out['results'] else None
    return Response({'run_id': out['summary']['run_id'], 'fy': fy, 'fy_start': out['summary']['fy_start'],
                     'fy_end': out['summary']['fy_end'], 'check': check_to_dict(check), 'result': result})


@api_view(['POST'])
def run_audit_view(request):
    data = request.data or {}
    fy = _fy(data)
    tenant_id = str(data.get('tenant_id') or KLIKK_TENANT_ID)
    codes = data.get('codes') or None
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.split(',') if c.strip()]
    out = run_audit(fy=fy, tenant_id=tenant_id, codes=codes,
                    triggered_by=str(data.get('triggered_by') or 'api:run_yearend_audit'))
    include_samples = bool(data.get('include_samples', True))
    sample_limit = _int(data.get('sample_limit'), 10, 0, 50)
    results = []
    for r in out['results']:
        r2 = dict(r)
        if not include_samples:
            r2.pop('sample_rows', None)
        else:
            r2['sample_rows'] = (r.get('sample_rows') or [])[:sample_limit]
        results.append(r2)
    return Response({'summary': out['summary'], 'results': results})


@api_view(['GET'])
def history_view(request, code):
    if not AuditCheck.objects.filter(code=code.upper()).exists():
        return Response({'detail': f'unknown check {code}'}, status=status.HTTP_404_NOT_FOUND)
    limit = _int(request.query_params.get('limit'), 20, 1, 200)
    fy = _int(request.query_params.get('fy'), None, 2015, 2100)
    return Response({'code': code.upper(), 'count': None, 'history': history_for(code, limit=limit, fy=fy)})


@api_view(['GET'])
def runs_view(request):
    limit = _int(request.query_params.get('limit'), 20, 1, 200)
    runs = AuditCheckRun.objects.all().order_by('-run_id')[:limit]
    return Response({'runs': [
        {'run_id': r.run_id, 'fy': r.fy, 'tenant_id': r.tenant_id,
         'started_at': r.started_at.isoformat() if r.started_at else None,
         'finished_at': r.finished_at.isoformat() if r.finished_at else None,
         'triggered_by': r.triggered_by,
         'counts': (r.summary or {}).get('counts'), 'checks_run': (r.summary or {}).get('checks_run')}
        for r in runs
    ]})
