"""
Comments pinned to a cube-view intersection.

This is OUR register, in our Postgres -- it never touches Xero. A comment is
anchored to the cell it was written on: the measure, the row path, the column
path and the filter context that produced the number. That anchor is what lets
an agent read a comment months later and still know exactly which figure it is
about, and turn it into a to-do.

Storage is raw SQL over app.cube_comments (the same pattern the audit registry
uses) so this needs no Django migration in a tree several agents are committing
to tonight.
"""
import hashlib
import json
import logging

from django.db import connection
from rest_framework import status as http
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)

DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.cube_comments (
    id          bigserial PRIMARY KEY,
    cell_key    text        NOT NULL UNIQUE,
    tenant_id   text        NOT NULL DEFAULT '',
    measure     text        NOT NULL,
    row_dims    text[]      NOT NULL,
    row_path    text[]      NOT NULL,
    col_dims    text[]      NOT NULL DEFAULT '{}',
    col_path    text        NOT NULL DEFAULT '',
    filters     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    cell_value  numeric(30,2),
    comment     text        NOT NULL,
    author      text        NOT NULL DEFAULT '',
    status      text        NOT NULL DEFAULT 'open',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cube_comments_status_idx ON app.cube_comments (status);
CREATE INDEX IF NOT EXISTS cube_comments_tenant_idx ON app.cube_comments (tenant_id);
"""

_ready = False


def _ensure_table():
    global _ready
    if _ready:
        return
    with connection.cursor() as c:
        c.execute(DDL)
    _ready = True


def _norm_measure(m):
    """'Sum of Amount', 'amount', 'Amount' -> 'amount'.

    A PivotTable names its measure the way Excel labels it; the cube names it
    by key. Same figure either way, so the anchor must not care.
    """
    m = (m or '').strip().lower()
    for prefix in ('sum of ', 'count of ', 'total of '):
        if m.startswith(prefix):
            m = m[len(prefix):]
    return m.strip() or 'amount'


def _coords(row_dims, row_path, col_dims, col_path):
    """The intersection as {dimension: value}, independent of axis.

    A cell is identified by WHICH dimensions take WHICH values -- not by
    whether the user dragged a field to rows or to columns. Anchoring on the
    axis meant moving Financial year from columns to rows silently orphaned
    every comment on the sheet: same number, different key. (app.cube_comments
    ids 35 and 37 are the same figure stored twice, which is how this was
    found.)
    """
    c = {}
    for d, v in zip(list(row_dims or []), list(row_path or [])):
        c[str(d)] = str(v)
    cp = (col_path or '').strip()
    if cp and cp != 'Total':
        parts = cp.split(' | ')
        for d, v in zip(list(col_dims or []), parts):
            c[str(d)] = str(v)
    return c


def _cell_key(tenant, measure, row_dims, row_path, col_dims, col_path, filters):
    """Stable identity for an intersection.

    Deliberately includes the filter context: the same coordinates under a
    different journal_type, date window or dimension filter is a different
    number, so a comment written about one must not silently reattach to the
    other.

    Deliberately EXCLUDES the axis layout, for the reason in _coords.
    """
    payload = json.dumps({
        'tenant': tenant or '',
        'measure': _norm_measure(measure),
        'coords': _coords(row_dims, row_path, col_dims, col_path),
        'filters': {k: v for k, v in sorted((filters or {}).items()) if v not in (None, '')},
    }, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


COLS = ('id, cell_key, tenant_id, measure, row_dims, row_path, col_dims, col_path, '
        'filters, cell_value, comment, author, status, created_at, updated_at')


def _row_to_dict(r):
    return {
        'id': r[0], 'cell_key': r[1], 'tenant_id': r[2], 'measure': r[3],
        'row_dims': list(r[4] or []), 'row_path': list(r[5] or []),
        'col_dims': list(r[6] or []), 'col_path': r[7],
        'filters': r[8], 'cell_value': float(r[9]) if r[9] is not None else None,
        'comment': r[10], 'author': r[11], 'status': r[12],
        'created_at': r[13].isoformat() if r[13] else None,
        'updated_at': r[14].isoformat() if r[14] else None,
    }


class XeroCubeCommentsView(APIView):
    """
    GET  /xero/data/journals/pivot/comments/
         status=open|actioned|dismissed|all  tenant=  measure=  limit=
         Returns the comments an agent should act on. Default status=open.

    POST /xero/data/journals/pivot/comments/
         {measure, row_dims[], row_path[], col_dims[], col_path, filters{},
          cell_value, comment, author, status?}
         Upserts on the intersection: one live comment per cell, re-posting
         edits it rather than accumulating duplicates. An empty comment deletes.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _ensure_table()
        p = request.query_params
        where, args = [], []

        st = (p.get('status') or 'open').strip()
        if st != 'all':
            where.append('status = %s')
            args.append(st)
        for param, col in (('tenant', 'tenant_id'), ('measure', 'measure')):
            val = (p.get(param) or '').strip()
            if val:
                where.append('%s = %%s' % col)
                args.append(val)

        try:
            limit = min(max(int(p.get('limit', 500)), 1), 5000)
        except (TypeError, ValueError):
            limit = 500

        sql = 'SELECT %s FROM app.cube_comments' % COLS
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY updated_at DESC LIMIT %s'
        args.append(limit)

        with connection.cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()
        return Response({'count': len(rows), 'results': [_row_to_dict(r) for r in rows]})

    def post(self, request):
        _ensure_table()
        d = request.data or {}

        measure = (d.get('measure') or '').strip()
        row_dims = d.get('row_dims') or []
        row_path = d.get('row_path') or []
        if not measure or not row_dims or not row_path:
            return Response({'error': 'measure, row_dims and row_path are required'},
                            status=http.HTTP_400_BAD_REQUEST)

        col_dims = d.get('col_dims') or []
        col_path = (d.get('col_path') or '').strip()
        filters = d.get('filters') or {}
        tenant = (filters.get('tenant') or '').strip()
        comment = (d.get('comment') or '').strip()

        key = _cell_key(tenant, measure, row_dims, row_path, col_dims, col_path, filters)

        # An emptied note means "retract", not "store a blank".
        if not comment:
            with connection.cursor() as c:
                c.execute('DELETE FROM app.cube_comments WHERE cell_key = %s', [key])
                deleted = c.rowcount
            return Response({'deleted': deleted, 'cell_key': key})

        val = d.get('cell_value')
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None

        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comments '
                '(cell_key, tenant_id, measure, row_dims, row_path, col_dims, col_path, '
                ' filters, cell_value, comment, author, status) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (cell_key) DO UPDATE SET '
                '  comment = EXCLUDED.comment, cell_value = EXCLUDED.cell_value, '
                '  author = EXCLUDED.author, status = EXCLUDED.status, updated_at = now() '
                'RETURNING ' + COLS,
                [key, tenant, measure, list(row_dims), list(row_path), list(col_dims),
                 col_path, json.dumps(filters), val, comment,
                 (d.get('author') or '').strip(), (d.get('status') or 'open').strip()],
            )
            row = c.fetchone()
        return Response(_row_to_dict(row), status=http.HTTP_200_OK)


class XeroCubeCommentStatusView(APIView):
    """POST /xero/data/journals/pivot/comments/<id>/status/  {status}

    For the agent that works the list: mark a comment actioned or dismissed
    without touching its text or its anchor.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, comment_id):
        _ensure_table()
        st = (request.data or {}).get('status', '').strip()
        if st not in ('open', 'actioned', 'dismissed'):
            return Response({'error': "status must be open, actioned or dismissed"},
                            status=http.HTTP_400_BAD_REQUEST)
        with connection.cursor() as c:
            c.execute('UPDATE app.cube_comments SET status = %s, updated_at = now() '
                      'WHERE id = %s RETURNING ' + COLS, [st, comment_id])
            row = c.fetchone()
        if not row:
            return Response({'error': 'no such comment'}, status=http.HTTP_404_NOT_FOUND)
        return Response(_row_to_dict(row))
