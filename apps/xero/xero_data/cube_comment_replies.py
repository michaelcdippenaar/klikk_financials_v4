"""
Replies on a comment in the register — ``app.cube_comment_replies``.

A cube comment is UPSERTED per (subject, author): one live note per person per
figure, so re-posting edits your own note. That is right for "my verdict on this
number" and useless for a conversation — an answer would overwrite the question.
Replies therefore get their own table, append-only, ordered by time.

The table itself is declared in ``pivot_comments.DDL`` (one place, right
creation order — the FK needs app.cube_comments to exist). This module owns the
QUERIES: everything that reads or writes a thread goes through here, so the
audit endpoints, the live feed and the Excel add-in all see the same rows in the
same shape.

Storage is raw SQL, matching the register it hangs off. There is no Django model
and no migration, deliberately: pivot_comments made that choice for the parent
table and a half-ORM half-SQL register would be worse than either.
"""
import logging

from django.db import connection

from apps.xero.xero_data import pivot_comments

logger = logging.getLogger(__name__)

# Columns of one reply, in the order the wire shape wants them.
REPLY_COLS = 'id, comment_id, parent_id, author, text, created_at'

# Inlining threads on a 2000-row list (the Excel add-in's page size) is bounded
# here rather than trusted to stay small: one runaway thread must not be able to
# make the add-in's fetch enormous. The count on each row is always exact, so a
# client can still tell that it is not seeing everything.
MAX_INLINE_REPLIES = 10_000

MAX_TEXT = 20_000


def ensure_table():
    """Both tables, in one call. See pivot_comments.DDL."""
    pivot_comments._ensure_table()


def _reply(row):
    """One reply on the wire. The SHAPE the whole contract is written against."""
    d = dict(zip([c.strip() for c in REPLY_COLS.split(',')], row))
    created = d['created_at']
    return {
        'id': d['id'],
        'parent_id': d['parent_id'],
        'author': d['author'],
        'text': d['text'],
        'created_at': created.isoformat() if created else None,
    }


def subject_ref(comment):
    """A comment in words: what the reply is ABOUT.

    Used for the activity trail's target_ref, the feed's object_ref and the
    webhook payload, so all three name the same figure the same way. Falls back
    through subject_label -> the cube coordinates -> the subject key, because a
    label is a nicety and must never be the reason a reply fails to post.
    """
    if not comment:
        return ''
    label = (comment.get('subject_label') or '').strip()
    if label:
        return label[:300]
    if comment.get('subject_type') == 'cube_cell':
        cube = pivot_comments._cube_label(comment.get('row_path'), comment.get('col_path'))
        if cube:
            return cube
    key = (comment.get('subject_key') or '')[:24]
    return ('%s %s' % (comment.get('subject_type') or 'comment', key)).strip()


def get_comment(comment_id):
    """The parent comment, or None. Enough of it to label the thread."""
    ensure_table()
    with connection.cursor() as c:
        c.execute(
            'SELECT id, subject_type, subject_key, subject_label, row_path, col_path, '
            '       comment, author, author_key, measure, cell_value '
            'FROM app.cube_comments WHERE id = %s',
            [comment_id],
        )
        row = c.fetchone()
    if not row:
        return None
    names = ('id', 'subject_type', 'subject_key', 'subject_label', 'row_path', 'col_path',
             'comment', 'author', 'author_key', 'measure', 'cell_value')
    out = dict(zip(names, row))
    out['row_path'] = list(out['row_path'] or [])
    return out


def fetch_replies(comment_id):
    """Every reply on one comment, OLDEST FIRST — a thread reads forwards."""
    ensure_table()
    with connection.cursor() as c:
        c.execute('SELECT %s FROM app.cube_comment_replies WHERE comment_id = %%s '
                  'ORDER BY created_at, id' % REPLY_COLS, [comment_id])
        return [_reply(r) for r in c.fetchall()]


def replies_for(comment_ids):
    """{comment_id: [reply, ...]} for many comments in ONE query.

    The list endpoints inline threads for up to 2000 comments; one query per
    comment would be 2000 round trips on a path the add-in calls on every sheet
    open.
    """
    ids = [int(i) for i in (comment_ids or [])]
    if not ids:
        return {}
    ensure_table()
    with connection.cursor() as c:
        c.execute('SELECT %s FROM app.cube_comment_replies WHERE comment_id = ANY(%%s) '
                  'ORDER BY comment_id, created_at, id LIMIT %%s' % REPLY_COLS,
                  [ids, MAX_INLINE_REPLIES])
        rows = c.fetchall()
    if len(rows) >= MAX_INLINE_REPLIES:
        logger.warning('replies_for hit the inline cap (%s) for %s comments — '
                       'some threads are truncated in this response',
                       MAX_INLINE_REPLIES, len(ids))
    out = {}
    for row in rows:
        reply = _reply(row)
        out.setdefault(row[1], []).append(reply)
    return out


def parent_error(comment_id, raw):
    """(parent_id, error) for an optional ``parent_id`` on this comment.

    A parent from ANOTHER comment is REJECTED rather than silently ignored:
    quietly re-homing a reply onto the wrong figure is how a thread ends up
    saying something nobody said.

    Absent means top-level. Note that ``None`` sent explicitly is treated the
    same as absent — the contract asks the client to omit the key, and refusing
    an explicit null would be a distinction without a difference to a JSON
    client that fills its object out.
    """
    if raw is None:
        return None, None
    if isinstance(raw, bool):  # bools are ints; True would resolve reply id 1
        return None, 'parent_id must be a reply on this comment'
    try:
        parent_id = int(raw)
    except (TypeError, ValueError):
        return None, 'parent_id must be a reply on this comment'
    with connection.cursor() as c:
        c.execute('SELECT 1 FROM app.cube_comment_replies WHERE id = %s AND comment_id = %s',
                  [parent_id, comment_id])
        found = c.fetchone()
    if not found:
        return None, 'parent_id must be a reply on this comment'
    return parent_id, None


def find_identical(comment_id, author, text):
    """An existing reply with the same author and text, or None.

    Only for callers that ask for it (the Excel sync). Two identical replies by
    one person on one figure are, in practice, a re-sync rather than someone
    saying the same thing twice — but that IS a judgement, so it is opt-in and
    never the default.
    """
    ensure_table()
    with connection.cursor() as c:
        c.execute('SELECT %s FROM app.cube_comment_replies '
                  'WHERE comment_id = %%s AND btrim(author) = btrim(%%s) '
                  '  AND btrim(text) = btrim(%%s) '
                  'ORDER BY created_at, id LIMIT 1' % REPLY_COLS,
                  [comment_id, author, text])
        row = c.fetchone()
    return _reply(row) if row else None


def create_reply(comment_id, author, text, parent_id=None):
    """Append one reply. The caller has already validated the parent."""
    ensure_table()
    with connection.cursor() as c:
        c.execute(
            'INSERT INTO app.cube_comment_replies (comment_id, parent_id, author, text) '
            'VALUES (%s,%s,%s,%s) RETURNING ' + REPLY_COLS,
            [comment_id, parent_id, author, text],
        )
        return _reply(c.fetchone())


def replies_since(since, limit):
    """Replies created after ``since``, oldest first, for the live comment feed.

    Joined to the parent comment so the feed can name the figure without a
    second query per event.
    """
    ensure_table()
    with connection.cursor() as c:
        c.execute(
            'SELECT r.id, r.comment_id, r.parent_id, r.author, r.text, r.created_at, '
            '       c.subject_type, c.subject_key, c.subject_label, c.row_path, c.col_path '
            'FROM app.cube_comment_replies r '
            'JOIN app.cube_comments c ON c.id = r.comment_id '
            'WHERE r.created_at > %s ORDER BY r.created_at, r.id LIMIT %s',
            [since, limit],
        )
        rows = c.fetchall()
    out = []
    for row in rows:
        reply = _reply(row[:6])
        out.append({
            'reply': reply,
            'comment_id': row[1],
            'created_at': row[5],
            'ref': subject_ref({
                'subject_type': row[6], 'subject_key': row[7], 'subject_label': row[8],
                'row_path': list(row[9] or []), 'col_path': row[10],
            }),
        })
    return out
