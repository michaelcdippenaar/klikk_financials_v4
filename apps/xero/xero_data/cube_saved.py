"""
Saved subsets and saved views -- the Planning Analytics idea, in our register.

A SUBSET is a named, ORDERED list of members of one dimension ("Trading
entities", "FY2024-FY2026"). A VIEW is a whole cube layout: rows, columns,
filters, measure, options, and the journal filter context that produced it.

Both are stored server-side rather than in the workbook or in localStorage,
for the same reason Planning Analytics does it: a subset is a piece of shared
analytical vocabulary. "The trading entities" should mean the same three
entities to MC, to the bookkeeper, and to an agent -- not three private
definitions that quietly drift.

Ordering is preserved exactly as saved: a subset is a sequence, not a set. The
cube honours it when laying out rows and columns, so "Revenue before Expenses"
stays that way on every rebuild.

Raw SQL over app.*, following the pattern already used by the audit registry
and cube comments, so this needs no migration in a tree several agents are
committing to.
"""
import json
import logging

from django.db import connection
from rest_framework import status as http
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.xero.xero_data import pivot_comments

logger = logging.getLogger(__name__)

DDL = """
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.cube_subsets (
    id         bigserial PRIMARY KEY,
    dimension  text        NOT NULL,
    name       text        NOT NULL,
    members    text[]      NOT NULL,
    author     text        NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dimension, name)
);
CREATE INDEX IF NOT EXISTS cube_subsets_dim_idx ON app.cube_subsets (dimension);

CREATE TABLE IF NOT EXISTS app.cube_views (
    id         bigserial PRIMARY KEY,
    name       text        NOT NULL UNIQUE,
    spec       jsonb       NOT NULL,
    query      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    author     text        NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

_ready = False


def _ensure():
    global _ready
    if _ready:
        return
    with connection.cursor() as c:
        c.execute(DDL)
    _ready = True


def _jsonb(v):
    """psycopg2 hands jsonb back as dict, but a text column round-trips as str."""
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


def _saved_by(request, declared):
    """Who saved this subset or view.

    The SAME answer pivot_comments gives for a comment author -- the pane fed
    both from one "Your name" box, and that box is gone. Sharing the resolver
    rather than re-deriving it here is the point: two answers to "who is this"
    is how one surface ends up stamped with a name the other would not accept.
    """
    return pivot_comments._author_identity(request, declared)[1]


class XeroCubeSubsetsView(APIView):
    """GET  /xero/data/journals/pivot/subsets/?dimension=fin_year
       POST {dimension, name, members[], author}   -- upsert, order preserved
       DELETE ?dimension=&name=
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _ensure()
        dim = (request.query_params.get('dimension') or '').strip()
        sql = ('SELECT id, dimension, name, members, author, updated_at '
               'FROM app.cube_subsets')
        args = []
        if dim:
            sql += ' WHERE dimension = %s'
            args.append(dim)
        sql += ' ORDER BY dimension, name'
        with connection.cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()
        return Response({'count': len(rows), 'results': [{
            'id': r[0], 'dimension': r[1], 'name': r[2],
            'members': list(r[3] or []), 'author': r[4],
            'updated_at': r[5].isoformat() if r[5] else None,
        } for r in rows]})

    def post(self, request):
        _ensure()
        d = request.data or {}
        dim = (d.get('dimension') or '').strip()
        name = (d.get('name') or '').strip()
        members = d.get('members') or []
        if not dim or not name:
            return Response({'error': 'dimension and name are required'},
                            status=http.HTTP_400_BAD_REQUEST)
        if not isinstance(members, list) or not members:
            return Response({'error': 'members must be a non-empty list -- an empty '
                                      'subset is the same as no subset, so there is '
                                      'nothing to save'},
                            status=http.HTTP_400_BAD_REQUEST)
        members = [str(m) for m in members]
        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_subsets (dimension, name, members, author) '
                'VALUES (%s,%s,%s,%s) '
                'ON CONFLICT (dimension, name) DO UPDATE SET '
                '  members = EXCLUDED.members, author = EXCLUDED.author, '
                '  updated_at = now() '
                'RETURNING id, dimension, name, members, author',
                [dim, name, members, _saved_by(request, d.get('author'))])
            r = c.fetchone()
        return Response({'id': r[0], 'dimension': r[1], 'name': r[2],
                         'members': list(r[3] or []), 'author': r[4]})

    def delete(self, request):
        _ensure()
        p = request.query_params
        dim = (p.get('dimension') or '').strip()
        name = (p.get('name') or '').strip()
        if not dim or not name:
            return Response({'error': 'dimension and name are required'},
                            status=http.HTTP_400_BAD_REQUEST)
        with connection.cursor() as c:
            c.execute('DELETE FROM app.cube_subsets WHERE dimension = %s AND name = %s',
                      [dim, name])
            n = c.rowcount
        return Response({'deleted': n})


class XeroCubeViewsView(APIView):
    """GET  /xero/data/journals/pivot/views/
       POST {name, spec{}, query{}, author}   -- upsert by name
       DELETE ?name=

    A view is the whole layout, INCLUDING the journal filter context. Saving
    the layout without the filters would produce a view that renders different
    numbers depending on what the pane happened to be filtered to when it was
    opened, which is the opposite of a saved view.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _ensure()
        with connection.cursor() as c:
            c.execute('SELECT id, name, spec, query, author, updated_at '
                      'FROM app.cube_views ORDER BY name')
            rows = c.fetchall()
        return Response({'count': len(rows), 'results': [{
            'id': r[0], 'name': r[1], 'spec': _jsonb(r[2]), 'query': _jsonb(r[3]),
            'author': r[4], 'updated_at': r[5].isoformat() if r[5] else None,
        } for r in rows]})

    def post(self, request):
        _ensure()
        d = request.data or {}
        name = (d.get('name') or '').strip()
        spec = d.get('spec')
        if not name or not isinstance(spec, dict):
            return Response({'error': 'name and spec are required'},
                            status=http.HTTP_400_BAD_REQUEST)
        if not (spec.get('rows') or []):
            return Response({'error': 'a view needs at least one row dimension'},
                            status=http.HTTP_400_BAD_REQUEST)
        query = d.get('query') if isinstance(d.get('query'), dict) else {}
        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_views (name, spec, query, author) '
                'VALUES (%s,%s,%s,%s) '
                'ON CONFLICT (name) DO UPDATE SET spec = EXCLUDED.spec, '
                '  query = EXCLUDED.query, author = EXCLUDED.author, updated_at = now() '
                'RETURNING id, name',
                [name, json.dumps(spec), json.dumps(query), _saved_by(request, d.get('author'))])
            r = c.fetchone()
        return Response({'id': r[0], 'name': r[1]})

    def delete(self, request):
        _ensure()
        name = (request.query_params.get('name') or '').strip()
        if not name:
            return Response({'error': 'name is required'}, status=http.HTTP_400_BAD_REQUEST)
        with connection.cursor() as c:
            c.execute('DELETE FROM app.cube_views WHERE name = %s', [name])
            n = c.rowcount
        return Response({'deleted': n})
