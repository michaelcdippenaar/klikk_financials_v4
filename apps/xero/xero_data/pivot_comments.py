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

from apps.xero.xero_data import cube_mentions

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

-- Comments are PER AUTHOR. The cell is shared; the note about it is not, so
-- two people reviewing the same figure must not overwrite each other. Uniqueness
-- is therefore (cell, author), not (cell).
ALTER TABLE app.cube_comments ADD COLUMN IF NOT EXISTS author_key text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS cube_comments_author_idx ON app.cube_comments (author_key);
ALTER TABLE app.cube_comments DROP CONSTRAINT IF EXISTS cube_comments_cell_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS cube_comments_cell_author_uq
    ON app.cube_comments (cell_key, author_key);

-- Tags relate a comment to a piece of work rather than to a cell: tag=audit is
-- how the year-end audit agent pulls exactly its own queue instead of reading
-- the whole register. GIN because the filter is containment (@>), not equality.
ALTER TABLE app.cube_comments ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}';

-- One register for every kind of subject, not one table per feature.
--
-- A comment is a note by someone, about something, that can be tagged, worked
-- and closed. None of that is specific to a cube cell -- so the SUBJECT became
-- a pair (type, key) and everything else stayed. A bank transaction, a journal
-- line, a slip and a cube cell now share one queue, one tag vocabulary, one
-- status lifecycle and one set of tools.
--
-- subject_key must be an identity that SURVIVES A RESYNC. For a cube cell that
-- is the coordinate hash; for a bank transaction it is Investec's uuid (or the
-- fallback hash when the API omits one). Never a row id that a reload could
-- reassign, and never a position in a list.
-- A VERDICT, separate from the note and from the workflow status.
--
-- Triaging bank transactions asks a specific question -- does this belong in
-- the company's books? -- and the answer is a small fixed vocabulary, not
-- prose. Kept apart from `status` (which tracks whether the item has been
-- WORKED) and from `tags` (freeform, and freeform fragments on typos:
-- "business-expense", "business expense", "buisness" are three tags and one
-- meaning). A field with a vocabulary can be counted, filtered and reported.
ALTER TABLE app.cube_comments ADD COLUMN IF NOT EXISTS decision text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS cube_comments_decision_idx ON app.cube_comments (decision)
    WHERE decision <> '';
ALTER TABLE app.cube_comments ADD COLUMN IF NOT EXISTS subject_type text NOT NULL DEFAULT 'cube_cell';
ALTER TABLE app.cube_comments ADD COLUMN IF NOT EXISTS subject_key  text NOT NULL DEFAULT '';
ALTER TABLE app.cube_comments ADD COLUMN IF NOT EXISTS subject_label text NOT NULL DEFAULT '';
UPDATE app.cube_comments SET subject_key = cell_key WHERE subject_key = '';
CREATE INDEX IF NOT EXISTS cube_comments_subject_idx ON app.cube_comments (subject_type, subject_key);
DROP INDEX IF EXISTS app.cube_comments_cell_author_uq;
CREATE UNIQUE INDEX IF NOT EXISTS cube_comments_subject_author_uq
    ON app.cube_comments (subject_type, subject_key, author_key);
CREATE INDEX IF NOT EXISTS cube_comments_tags_gin ON app.cube_comments USING gin (tags);
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



MAX_TAGS = 20
MAX_TAG_LEN = 40


def _norm_tags(raw):
    """Normalise whatever the client sent into a clean, bounded tag list.

    Lowercased, trimmed, a leading '#' dropped, empties removed, duplicates
    collapsed, order preserved. Bounded on BOTH axes -- at most MAX_TAGS tags of
    at most MAX_TAG_LEN chars -- because this column is written straight from a
    client payload and an unbounded text[] is how one malformed add-in build
    fills the register with a thousand junk tags that nobody can clear from
    Excel.

    Accepts a list or a comma-separated string, since the pane and the MCP
    naturally send different shapes.
    """
    if raw is None:
        return None                      # absent != empty: absent means "leave as is"
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, (list, tuple)):
        return []
    out, seen = [], set()
    for item in raw:
        tag = str(item or '').strip().lstrip('#').strip().lower()
        if not tag:
            continue
        tag = tag[:MAX_TAG_LEN]
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def _author_identity(request, declared):
    """Who wrote this comment.

    The authenticated user is the authority. The add-in currently signs in as
    the shared `excel-addin` service account, so that name identifies the TOOL,
    not the person -- when that is who we are, fall back to the name typed in
    the pane and mark it self-declared, rather than filing everyone's notes
    under one identity.

    Give each person their own login and this collapses to the real username
    with verified=True, and nothing else has to change.
    """
    user = getattr(request, 'user', None)
    username = getattr(user, 'username', '') or ''
    declared = (declared or '').strip()
    # 'service-token' is the shared MCP credential and 'excel-addin' the shared
    # add-in login: both name a TOOL, not a person. An agent writing a comment
    # must say who it is, or the queue fills with notes from 'service-token'
    # and nobody can tell the auditor's from the sync job's.
    SHARED = {'excel-addin', 'service-token', ''}
    if username and username not in SHARED:
        return username, username, True
    return (declared or 'unattributed'), declared, False


COLS = ('id, cell_key, subject_type, subject_key, subject_label, tenant_id, measure, row_dims, row_path, col_dims, col_path, '
        'filters, cell_value, comment, author, author_key, status, decision, created_at, updated_at, '
        'tags')


def _jsonb(v):
    """jsonb comes back as a dict on some paths and as a JSON STRING on others.

    Same reason cube_saved.py carries this helper. Leaving it raw is not
    cosmetic: every consumer that treats `filters` as a mapping breaks on the
    string form. It made the MCP return filter_context as a string, and it made
    the mention email raise AttributeError mid-send -- which the notify() guard
    caught and recorded, but the fix belongs HERE, at the source, rather than in
    each caller.
    """
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}



def _cube_label(row_path, col_path):
    """A cube cell in words, so a mixed queue is readable without joining back."""
    bits = ' / '.join(str(x) for x in (row_path or []))
    if col_path and col_path != 'Total':
        bits += ' \u00d7 ' + str(col_path)
    return bits[:300]



DECISIONS = {
    'business_expense': 'A company cost, and it belongs in the books',
    'personal': 'Not the company\'s — settle to the loan account',
    'duplicate': 'Already captured elsewhere',
    'needs_info': 'Cannot be decided without something else',
    'no_action': 'Looked at, nothing to do',
}


def _norm_decision(raw):
    """Accept the vocabulary, and the obvious spellings of it.

    An agent or a person will write "business expense" or "business-expense";
    refusing those would push people back to freeform tags, which is the thing
    this field exists to replace.
    """
    d = str(raw or '').strip().lower().replace('-', '_').replace(' ', '_')
    if not d:
        return ''
    if d in DECISIONS:
        return d
    aliases = {
        'business': 'business_expense', 'company': 'business_expense',
        'expense': 'business_expense', 'klikk': 'business_expense',
        'private': 'personal', 'own': 'personal', 'loan_account': 'personal',
        'dup': 'duplicate', 'info': 'needs_info', 'unknown': 'needs_info',
        'none': 'no_action', 'ok': 'no_action',
    }
    if d in aliases:
        return aliases[d]
    raise ValueError('unknown decision %r — use one of: %s'
                     % (raw, ', '.join(sorted(DECISIONS))))


SUBJECT_KINDS = {
    'cube_cell': 'A figure in a cube or PivotTable',
    'bank_txn': 'A transaction on a bank account, as the bank sent it',
    'journal_line': 'One line of a Xero journal',
    'slip': 'A receipt in the Slippies register',
    'invoice': 'A Xero invoice',
}


COL_NAMES = [c.strip() for c in COLS.split(',')]


def _row_to_dict(r):
    """Map a row by COLUMN NAME, not by position.

    This was a list of hand-numbered indexes, so adding a column to COLS
    silently shifted every field after it -- comment text landing in the author
    field, and so on. Names cost nothing and cannot drift out of step with the
    SELECT they came from.
    """
    d = dict(zip(COL_NAMES, r))
    return {
        'id': d['id'],
        'cell_key': d['cell_key'],
        'subject_type': d['subject_type'],
        'subject_key': d['subject_key'],
        'subject_label': d['subject_label'],
        'tenant_id': d['tenant_id'],
        'measure': d['measure'],
        'row_dims': list(d['row_dims'] or []),
        'row_path': list(d['row_path'] or []),
        'col_dims': list(d['col_dims'] or []),
        'col_path': d['col_path'],
        'filters': _jsonb(d['filters']),
        'cell_value': float(d['cell_value']) if d['cell_value'] is not None else None,
        'comment': d['comment'],
        'author': d['author'],
        'author_key': d['author_key'],
        'status': d['status'],
        'decision': d['decision'],
        'tags': list(d['tags'] or []),
        'created_at': d['created_at'].isoformat() if d['created_at'] else None,
        'updated_at': d['updated_at'].isoformat() if d['updated_at'] else None,
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

        # This endpoint is the CUBE's view of the register. Now that the same
        # table also holds bank transactions (and will hold more kinds), it has
        # to say so — otherwise the add-in fetches comments it can never place
        # on a sheet, and its counts describe a bigger queue than it shows.
        where.append('subject_type = %s')
        args.append('cube_cell')

        st = (p.get('status') or 'open').strip()
        if st != 'all':
            where.append('status = %s')
            args.append(st)
        author = (p.get('author') or '').strip()
        if author:
            where.append('author_key = %s')
            args.append(author)

        for param, col in (('tenant', 'tenant_id'), ('measure', 'measure')):
            val = (p.get(param) or '').strip()
            if val:
                where.append('%s = %%s' % col)
                args.append(val)

        # tag=audit           -> has that tag
        # tags=audit,fy2026   -> has ALL of them (containment, not overlap:
        #                        narrowing a queue is the point, and && would
        #                        widen it instead)
        wanted = _norm_tags(p.get('tags')) or []
        single = _norm_tags(p.get('tag')) or []
        wanted = wanted + [t for t in single if t not in wanted]
        if wanted:
            where.append('tags @> %s')
            args.append(list(wanted))

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

        author_key, author_name, verified = _author_identity(request, d.get('author'))
        key = _cell_key(tenant, measure, row_dims, row_path, col_dims, col_path, filters)

        # An emptied note means "retract", not "store a blank" -- and retracts
        # only YOUR note on that cell, never anyone else's.
        if not comment:
            with connection.cursor() as c:
                c.execute('DELETE FROM app.cube_comments WHERE subject_type = %s '
                          'AND subject_key = %s AND author_key = %s',
                          ['cube_cell', key, author_key])
                deleted = c.rowcount
            return Response({'deleted': deleted, 'cell_key': key, 'author_key': author_key})

        val = d.get('cell_value')
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None

        tags = _norm_tags(d.get('tags'))
        if tags is None:
            tags = []

        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comments '
                '(cell_key, tenant_id, measure, row_dims, row_path, col_dims, col_path, '
                ' filters, cell_value, comment, author, author_key, status, tags) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (cell_key, author_key) DO UPDATE SET '
                '  comment = EXCLUDED.comment, cell_value = EXCLUDED.cell_value, '
                '  author = EXCLUDED.author, status = EXCLUDED.status, '
                '  tags = EXCLUDED.tags, updated_at = now() '
                'RETURNING ' + COLS,
                [key, tenant, measure, list(row_dims), list(row_path), list(col_dims),
                 col_path, json.dumps(filters), val, comment,
                 author_name, author_key, (d.get('status') or 'open').strip(), tags],
            )
            row = c.fetchone()
        out = _row_to_dict(row)
        out['author_verified'] = verified

        # The comment is saved and committed by this point. Notification is
        # strictly best-effort from here on: notify() catches everything and
        # reports it, so a dead mail server costs an email, never the comment.
        out['mentions'] = cube_mentions.notify(
            out,
            _coords(row_dims, row_path, col_dims, col_path),
            author_name,
            cube_mentions.parse_mentions(comment),
        )
        return Response(out, status=http.HTTP_200_OK)


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


MAX_BULK = 1000


class XeroCubeCommentsBulkView(APIView):
    """POST /xero/data/journals/pivot/comments/bulk/

    Flag many cells at once.

    Reviewing a cube means spotting a dozen figures that look wrong, and
    writing a considered note on each is not what that moment needs -- the
    useful action is "these ones, check them". So one shared note and one set
    of tags are applied across a selection, and the detail is written later on
    the few that turn out to matter.

    One request, one transaction. The obvious alternative -- the client posting
    each cell in turn -- is a round trip per cell, so flagging sixty cells
    would take sixty of them and fail halfway through often enough to matter.

    Every cell still gets its OWN anchor and its own row. This is a bulk write,
    not a group object: untagging one, commenting properly on another, or
    actioning a third all behave exactly as they do for a comment written by
    hand.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        _ensure_table()
        d = request.data or {}
        cells = d.get('cells')
        if not isinstance(cells, list) or not cells:
            return Response({'error': 'cells must be a non-empty list'},
                            status=http.HTTP_400_BAD_REQUEST)
        if len(cells) > MAX_BULK:
            return Response({'error': 'at most %d cells at a time (got %d)'
                                      % (MAX_BULK, len(cells))},
                            status=http.HTTP_400_BAD_REQUEST)

        shared_comment = (d.get('comment') or '').strip()
        shared_tags = _norm_tags(d.get('tags'))
        author_key, author_name, verified = _author_identity(request, d.get('author'))
        status_val = (d.get('status') or 'open').strip()

        saved, skipped, results = 0, [], []
        with connection.cursor() as c:
            for i, cell in enumerate(cells):
                measure = (cell.get('measure') or d.get('measure') or '').strip()
                row_dims = cell.get('row_dims') or []
                row_path = cell.get('row_path') or []
                comment = (cell.get('comment') or shared_comment).strip()
                if not measure or not row_dims or not row_path:
                    skipped.append({'index': i, 'why': 'missing measure, row_dims or row_path'})
                    continue
                if not comment:
                    # A bulk flag with no text at all would be invisible on the
                    # sheet and meaningless in the queue.
                    skipped.append({'index': i, 'why': 'no comment text'})
                    continue

                col_dims = cell.get('col_dims') or []
                col_path = (cell.get('col_path') or '').strip()
                filters = cell.get('filters') or d.get('filters') or {}
                tenant = (filters.get('tenant') or '').strip()
                tags = _norm_tags(cell.get('tags')) or shared_tags

                val = cell.get('cell_value')
                try:
                    val = float(val) if val is not None else None
                except (TypeError, ValueError):
                    val = None

                key = _cell_key(tenant, measure, row_dims, row_path,
                                col_dims, col_path, filters)
                c.execute(
                    'INSERT INTO app.cube_comments '
                    '(cell_key, tenant_id, measure, row_dims, row_path, col_dims, col_path, '
                    ' filters, cell_value, comment, author, author_key, status, tags) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                    'ON CONFLICT (cell_key, author_key) DO UPDATE SET '
                    '  comment = EXCLUDED.comment, cell_value = EXCLUDED.cell_value, '
                    '  tags = EXCLUDED.tags, status = EXCLUDED.status, updated_at = now() '
                    'RETURNING id',
                    [key, tenant, measure, list(row_dims), list(row_path), list(col_dims),
                     col_path, json.dumps(filters), val, comment,
                     author_name, author_key, status_val, tags],
                )
                results.append({'id': c.fetchone()[0], 'cell_key': key})
                saved += 1

        return Response({
            'saved': saved,
            'skipped': skipped,
            'author': author_name,
            'author_verified': verified,
            'tags': shared_tags,
            'results': results,
        })


class CommentsView(APIView):
    """GET/POST /xero/data/comments/  — the comment register, any subject.

    The generic face of the same table the cube comments live in. A comment is
    a note by someone, about something, that can be tagged, worked and closed;
    none of that was ever specific to a cube cell.

        GET  ?subject_type=bank_txn&subject_key=<uuid>
             ?subject_type=bank_txn            (all of that kind)
             ?status=open|actioned|dismissed|all  ?tag=  ?tags=a,b  ?author=
        POST {subject_type, subject_key, subject_label?, comment, author,
              tags[], status?, context{}}

    `subject_key` must be an identity that survives a resync -- Investec's uuid
    for a bank transaction, the coordinate hash for a cube cell. A database row
    id would look stable and quietly point at a different transaction after a
    reload.

    The cube keeps its own endpoint, which is a facade over this: it takes
    coordinates and computes the key. Two ways in, one register.
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

        for param, col in (('subject_type', 'subject_type'), ('subject_key', 'subject_key'),
                           ('author', 'author_key'), ('measure', 'measure'),
                           ('decision', 'decision')):
            val = (p.get(param) or '').strip()
            if val:
                where.append('%s = %%s' % col)
                args.append(val)

        tags = _norm_tags(
            (p.get('tags') or p.get('tag') or '').split(',')
        )
        if tags:
            where.append('tags @> %s')
            args.append(tags)

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
        subject_type = (d.get('subject_type') or '').strip()
        subject_key = (d.get('subject_key') or '').strip()
        if not subject_type or not subject_key:
            return Response(
                {'error': 'subject_type and subject_key are required',
                 'known_subject_types': SUBJECT_KINDS},
                status=http.HTTP_400_BAD_REQUEST)
        if subject_type == 'cube_cell':
            return Response({'error': 'post a cube-cell comment to '
                                      'journals/pivot/comments/, which builds the key '
                                      'from the coordinates'},
                            status=http.HTTP_400_BAD_REQUEST)

        comment = (d.get('comment') or '').strip()
        author_key, author_name, verified = _author_identity(request, d.get('author'))
        context = d.get('context') if isinstance(d.get('context'), dict) else {}
        try:
            decision = _norm_decision(d.get('decision'))
        except ValueError as exc:
            return Response({'error': str(exc), 'decisions': DECISIONS},
                            status=http.HTTP_400_BAD_REQUEST)

        if not comment:
            with connection.cursor() as c:
                c.execute('DELETE FROM app.cube_comments WHERE subject_type = %s '
                          'AND subject_key = %s AND author_key = %s',
                          [subject_type, subject_key, author_key])
                deleted = c.rowcount
            return Response({'deleted': deleted, 'subject_type': subject_type,
                             'subject_key': subject_key})

        val = d.get('value')
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None

        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comments '
                '(cell_key, subject_type, subject_key, subject_label, tenant_id, measure, '
                ' row_dims, row_path, col_dims, col_path, filters, cell_value, '
                ' comment, author, author_key, status, decision, tags) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (subject_type, subject_key, author_key) DO UPDATE SET '
                '  comment = EXCLUDED.comment, subject_label = EXCLUDED.subject_label, '
                '  tags = EXCLUDED.tags, cell_value = EXCLUDED.cell_value, '
                '  filters = EXCLUDED.filters, status = EXCLUDED.status, '
                '  decision = EXCLUDED.decision, updated_at = now() '
                'RETURNING ' + COLS,
                [
                    # cell_key stays UNIQUE-shaped for the legacy column; for a
                    # non-cube subject it is simply the subject key.
                    '%s:%s' % (subject_type, subject_key),
                    subject_type, subject_key, (d.get('subject_label') or '').strip()[:300],
                    (context.get('tenant') or '').strip(), (d.get('measure') or '').strip(),
                    [], [], [], '', json.dumps(context), val,
                    comment, author_name, author_key,
                    (d.get('status') or 'open').strip(), decision, _norm_tags(d.get('tags')),
                ],
            )
            row = c.fetchone()
        out = _row_to_dict(row)
        out['author_verified'] = verified
        return Response(out, status=http.HTTP_200_OK)
