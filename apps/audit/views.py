"""
Read-only views for the year-end audit check registry.

The registry lives in Postgres as `audit.checks` — one row per executable
check (see Klikk-YearEnd-Audit-Procedures.md §2). It is built and maintained
outside Django (plain SQL migrations owned by the audit tooling), so these
views deliberately use RAW SQL and define NO Django model and NO migration:
the table can appear, gain columns, or be rebuilt without this app caring.

If the table does not exist yet the endpoint returns an empty list with
`registry_ready: false` so the console can fall back to the static list of
planned checks instead of erroring.

STRICTLY READ-ONLY. Nothing in this module writes to any database.
"""
import logging

from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


# Columns we know how to serialise. We intersect this with the columns the
# table actually has, so a schema still being built (or later extended) does
# not break the endpoint.
KNOWN_COLUMNS = [
    'id',
    'code',
    'title',
    'category',
    'severity',
    'description',
    'rationale',
    'sql_text',
    'expected',
    'owner_action',
    'active',
    'created_at',
    'source',
]


class AuditChecksView(APIView):
    """
    GET /audit/checks/ — the year-end audit check registry, read-only.

    Mirrors the permission pattern of the neighbouring read-only status
    endpoints (e.g. /xero/sync/process-status/).

    Optional query params:
        category  — exact category match (case-insensitive)
        active    — 'true' / 'false' to filter on the active flag

    Response:
        {
          "registry_ready": true|false,
          "count": <int>,
          "categories": ["Data readiness", ...],
          "checks": [ { code, title, category, severity, description,
                        rationale, sql_text, expected, owner_action,
                        active, created_at, source }, ... ]
        }

    When `registry_ready` is false the table has not been created yet and
    `checks` is empty — the console then shows the static planned-check list.
    """

    permission_classes = [AllowAny]  # matches sibling read-only views

    def get(self, request):
        category = request.query_params.get('category')
        active = request.query_params.get('active')

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('audit.checks')")
                exists = cursor.fetchone()[0]
                if not exists:
                    return Response({
                        'registry_ready': False,
                        'count': 0,
                        'categories': [],
                        'checks': [],
                        'detail': 'audit.checks does not exist yet.',
                    })

                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'audit' AND table_name = 'checks'
                    """
                )
                present = {row[0] for row in cursor.fetchall()}
                columns = [c for c in KNOWN_COLUMNS if c in present]
                if not columns:
                    return Response({
                        'registry_ready': False,
                        'count': 0,
                        'categories': [],
                        'checks': [],
                        'detail': 'audit.checks exists but has no recognised columns.',
                    })

                where, params = [], []
                if category and 'category' in present:
                    where.append('lower(category) = lower(%s)')
                    params.append(category)
                if active is not None and 'active' in present:
                    where.append('active = %s')
                    params.append(str(active).lower() in ('1', 'true', 'yes'))

                select_list = ', '.join('"%s"' % c for c in columns)
                sql = 'SELECT %s FROM audit.checks' % select_list
                if where:
                    sql += ' WHERE ' + ' AND '.join(where)
                order = []
                if 'category' in present:
                    order.append('category')
                if 'code' in present:
                    order.append('code')
                if order:
                    sql += ' ORDER BY ' + ', '.join(order)

                cursor.execute(sql, params)
                rows = cursor.fetchall()
        except Exception:
            logger.exception('Failed to read audit.checks registry')
            return Response({
                'registry_ready': False,
                'count': 0,
                'categories': [],
                'checks': [],
                'detail': 'Could not read the audit registry.',
            })

        checks = []
        for row in rows:
            item = {}
            for key, value in zip(columns, row):
                item[key] = value.isoformat() if hasattr(value, 'isoformat') else value
            checks.append(item)

        categories = sorted({c['category'] for c in checks if c.get('category')})

        return Response({
            'registry_ready': True,
            'count': len(checks),
            'categories': categories,
            'checks': checks,
        })
