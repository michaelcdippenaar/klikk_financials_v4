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

from apps.user import identity
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

-- WHO SHOULD ACT -- which is not who wrote it, and not whether it is done.
--
-- The register gained a second kind of reader the moment a bookkeeper joined
-- the loop: MC raises a point against a figure, someone else answers it, and
-- MC closes it. That needs a fourth axis. `author_key` is who WROTE the note,
-- `status` is whether it has been WORKED, `decision` is the VERDICT -- and
-- none of the three can say who the point is waiting on. Overloading any of
-- them would make "my queue" unanswerable without guessing, which for a work
-- queue is the whole feature.
--
-- Stored RESOLVED (a username), never as the prose that produced it. An
-- '@anzelle' typed into a note is input; a queue rebuilt by re-reading prose
-- breaks the first time someone writes '@anzelle?' or an account is renamed,
-- and it breaks silently -- the point then sits in nobody's list while looking
-- assigned, which is worse than never having been assigned at all.
--
-- Partial index: nearly every row is assigned to nobody, and the only query
-- that matters is "the ones that are".
-- Named `assignee_role`, not `assignee`, because what is stored is a SEAT and
-- not a worker. Renamed from the `assignee_role` this shipped as a few hours
-- earlier, while every row still held '' -- a rename is free until the first
-- point is assigned and then it is a data migration across three consumers.
--
-- The name leaves room for an `assignee_person` beside it without renaming a
-- live column later. That column is deliberately NOT built: with one holder per
-- seat the person is derivable from the directory, and a second field that can
-- disagree with the first is worse than no field.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'app' AND table_name = 'cube_comments'
                 AND column_name = 'assignee_key')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = 'app' AND table_name = 'cube_comments'
                         AND column_name = 'assignee_role') THEN
        ALTER TABLE app.cube_comments RENAME COLUMN assignee_key TO assignee_role;
    END IF;
END $$;
ALTER TABLE app.cube_comments ADD COLUMN IF NOT EXISTS assignee_role text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS cube_comments_assignee_idx ON app.cube_comments (assignee_role)
    WHERE assignee_role <> '';

-- WHAT THE POINT IS ABOUT, frozen when it was raised.
--
-- The pilot's blocker: a bookkeeper can be named, assigned and mailed, but she
-- cannot SEE the transaction a point refers to. The lines live behind
-- /xero/data/journals/pivot/drill/, and everything under /xero/data/ is 403
-- for the auditor role she signs in as. The alternative to this table was
-- granting her the general ledger, which is a much larger decision than the
-- pilot needs and cannot be undone quietly.
--
-- So the point CARRIES its evidence instead: the lines and their receipt links
-- are resolved once, at raise time, and stored here. She sees what the point is
-- about; her access does not widen by one route.
--
-- Frozen ON PURPOSE, and this is the one place in the register where a stale
-- copy is the correct thing to hold. The live drill re-resolves from the anchor
-- because the lines behind a figure change when Xero is re-synced -- right for
-- "what is this figure now", wrong for "what was I looking at when I raised
-- this". `cell_value_at_capture` is the same idea: it is what makes
-- "it was -535,301.79, it is now -498,112.40" answerable later without anyone
-- typing either number.
--
-- Its own table, not a column: the captured lines are large and the register
-- list must not carry them. That list was 1.4 MB for 113 rows once and hung
-- the page.
CREATE TABLE IF NOT EXISTS app.cube_comment_context (
    comment_id   bigint      PRIMARY KEY REFERENCES app.cube_comments(id) ON DELETE CASCADE,
    coords       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    measure      text        NOT NULL DEFAULT '',
    lines        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    line_count   integer     NOT NULL DEFAULT 0,
    line_total   numeric(30,2),
    truncated    boolean     NOT NULL DEFAULT false,
    receipt_count integer    NOT NULL DEFAULT 0,
    cell_value_at_capture numeric(30,2),
    captured_by  text        NOT NULL DEFAULT '',
    -- clock_timestamp(), not now(): now() is TRANSACTION start time, so two
    -- captures in one transaction would carry the same stamp and "when did I
    -- last look at this" would be answered with a time that is not when.
    captured_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- WHAT A POINT USED TO SAY.
--
-- The register is not a notes field. It is the human->agent work queue, and
-- agents act on what it says -- which is why the reply module is append-only
-- with no PATCH and no DELETE anywhere in it. Editing cuts against that, so it
-- is reconciled explicitly rather than by accident: a comment an agent has
-- already acted on, silently rewritten, means the trail no longer says what was
-- actually acted on.
--
-- So an edit RECORDS rather than replaces. Shaped after
-- app.cube_comment_assignments (from/to, who, when) rather than after
-- cube_comment_mentions (per-attempt with an outcome), because an edit is a
-- change of state with two sides and not an attempt that can fail. Both texts
-- are stored so one row reads on its own, without replaying the chain.
--
-- Only the TEXT is editable, and that is enforced by this endpoint accepting
-- nothing else. The anchor is identity -- re-anchoring is a MOVE, not an edit,
-- and it silently changes which figure the comment is about. `author_key` is
-- identity too: an admin correcting an agent's note must never re-stamp it, or
-- the register attributes to `claude:year-end-audit` words it never wrote.
CREATE TABLE IF NOT EXISTS app.cube_comment_edits (
    id           bigserial   PRIMARY KEY,
    comment_id   bigint      NOT NULL REFERENCES app.cube_comments(id) ON DELETE CASCADE,
    from_text    text        NOT NULL DEFAULT '',
    to_text      text        NOT NULL DEFAULT '',
    -- Who wrote the words originally, kept beside who changed them. An admin
    -- edit of an agent's note is the case this column exists to make legible.
    author_key   text        NOT NULL DEFAULT '',
    edited_by    text        NOT NULL DEFAULT '',
    edited_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS cube_comment_edits_comment_idx
    ON app.cube_comment_edits (comment_id, edited_at DESC);

-- WHERE A POINT HAS BEEN, and since when.
--
-- The comment row knows where a point is NOW and cannot answer "how long has
-- this been sitting with the bookkeeper" -- which is the number that says the
-- review loop has stalled, and the reason this table earns its place day to
-- day. The audit half matters at handover: `assignee_role` names a seat whose
-- holder changes, so without a log every point ever routed to `bookkeeper`
-- silently re-reads as whoever holds that seat later.
--
-- Append-only, and the INITIAL assignment gets a row like any other -- the
-- commonest case by far is raised once and never moved, and deriving history
-- from current membership is exactly what breaks when the membership changes.
-- It cannot be backfilled: a day without it is a day of ageing lost for good.
CREATE TABLE IF NOT EXISTS app.cube_comment_assignments (
    id           bigserial   PRIMARY KEY,
    comment_id   bigint      NOT NULL REFERENCES app.cube_comments(id) ON DELETE CASCADE,
    from_role    text        NOT NULL DEFAULT '',
    to_role      text        NOT NULL DEFAULT '',
    -- Who held the seat at that moment. The seat cannot preserve it, and this
    -- is the question an auditor asks of a review register.
    held_by      text        NOT NULL DEFAULT '',
    held_by_email text       NOT NULL DEFAULT '',
    changed_by   text        NOT NULL DEFAULT '',
    changed_at   timestamptz NOT NULL DEFAULT now()
);
-- The ageing read is "this comment's moves, newest first"; the queue-wide read
-- is "everything that landed on this seat and when".
CREATE INDEX IF NOT EXISTS cube_comment_assignments_comment_idx
    ON app.cube_comment_assignments (comment_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS cube_comment_assignments_to_idx
    ON app.cube_comment_assignments (to_role, changed_at DESC)
    WHERE to_role <> '';

-- A comment is a CONVERSATION, not a note.
--
-- The register upserts one row per (subject, author): re-posting edits your own
-- note rather than accumulating duplicates, which is right for "my verdict on
-- this figure" and useless for "the auditor asked a question and someone
-- answered it". Replies therefore live in their own table -- append-only, never
-- upserted -- so a thread reads in the order it was written and nobody's answer
-- can overwrite anyone else's.
--
-- Declared HERE, in the register's own DDL, so the two tables are created in one
-- place and in the right order: the FK below needs app.cube_comments to exist.
CREATE TABLE IF NOT EXISTS app.cube_comment_replies (
    id          bigserial PRIMARY KEY,
    comment_id  bigint      NOT NULL REFERENCES app.cube_comments(id) ON DELETE CASCADE,
    -- Self-reference for reply-to-reply. ON DELETE CASCADE so deleting a reply
    -- takes its sub-thread with it rather than orphaning rows that point at a
    -- parent that is gone.
    parent_id   bigint      NULL REFERENCES app.cube_comment_replies(id) ON DELETE CASCADE,
    author      text        NOT NULL,
    text        text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cube_comment_replies_comment_idx
    ON app.cube_comment_replies (comment_id);
-- The thread read is always "this comment's replies, oldest first", and the
-- reply_count subquery on the list is "how many for this comment" -- both are
-- served by the composite, which is why it exists alongside the plain one.
CREATE INDEX IF NOT EXISTS cube_comment_replies_thread_idx
    ON app.cube_comment_replies (comment_id, created_at);
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


# Credentials that name a TOOL rather than a person: the shared MCP service
# token and the Excel add-in's login. Neither may be stamped on a comment as-is
# -- a register where half the notes are authored by 'service-token' cannot be
# filtered by who wrote them.
SHARED_CREDENTIALS = {'excel-addin', 'service-token', ''}


def _author_identity(request, declared):
    """Who wrote this comment. Returns (author_key, author_name, verified).

    Three kinds of caller, three answers, in this order:

    1. A SHARED CREDENTIAL WITH A KNOWN OPERATOR -- the Excel add-in. The
       account is mapped to a person in settings.SERVICE_ACCOUNT_OPERATORS, so
       the server stamps that person and IGNORES whatever the client sent. This
       is the case MC's complaint is about: the pane used to make him type his
       name, and a free-text author box is a field that can disagree with the
       credential. app.cube_comments carries the receipts -- `ewffew` x12,
       `test`, `test2`, MC's own notes split across author_key 'MC' and '', and
       55 rows authored by nobody at all.

    2. A REAL PERSON signed in as themselves (the console JWT, an auditor's
       session). Their username is the answer, as it always was.

    3. A SHARED CREDENTIAL WITH NO OPERATOR -- the MCP service token. One
       credential, many agents, so the caller must say which workstream it is:
       'claude:year-end-audit' and 'codex:fy2026-account-allocation' are how MC
       tells them apart, and they keep working exactly as before. Marked
       verified=False, because a self-declared name is a claim, not a fact.

    The distinction that matters is (1) vs (3): an identity the SERVER knows is
    stamped; an identity only the CLIENT knows is accepted and labelled as
    such. Nothing here lets a caller of kind (1) opt back into kind (3).
    """
    user = getattr(request, 'user', None)
    username = getattr(user, 'username', '') or ''
    declared = (declared or '').strip()
    operator = identity.service_operator(user)
    if operator:
        return operator, operator, True
    if username and username not in SHARED_CREDENTIALS:
        return username, username, True
    return (declared or 'unattributed'), declared, False


COLS = ('id, cell_key, subject_type, subject_key, subject_label, tenant_id, measure, row_dims, row_path, col_dims, col_path, '
        'filters, cell_value, comment, author, author_key, assignee_role, status, decision, created_at, updated_at, '
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


def _resolve_assignee(raw, *, requester_email=''):
    """Resolve what a writer typed to a `app.cube_people` HANDLE, plus who holds it.

    Assignment names a ROLE, not a person: `bookkeeper`, not `anzelle`. The
    handle is what gets stored on the comment, and `app.cube_people` says who
    that is today -- so replacing a bookkeeper is one row in the directory
    instead of a rewrite of every point ever sent to her. Storing the person
    would throw that away the moment she is replaced.

    Resolved against the DIRECTORY and not the Django user table, deliberately.
    The people who receive work here have no login -- the mentions module is
    explicit that an address "must be entered on purpose" and never guessed --
    so resolving against users would mean nobody can be assigned until somebody
    creates them an account they do not need. It also keeps ONE answer to "who
    is the bookkeeper": mentions and assignment now read the same directory
    instead of drifting apart.

    An unknown handle is an error, never a quiet drop -- a typo'd role is work
    routed to a role nobody holds, and nothing anywhere reports it. An INACTIVE
    handle is refused for the same reason: assigning to a role that has been
    stood down is assigning to nobody. Rows already pointing at a stood-down
    handle are left exactly as they are; see `_current_assignee`.

    Returns (handle, holder) -- the holder being the snapshot recorded on the
    activity trail, because the comment itself cannot preserve it.
    """
    name = str(raw or '').strip().lstrip('@').lower()
    if not name:
        return '', {}
    cube_mentions.ensure_tables()
    if name == 'me':
        # 'me' has no meaning in a directory of roles unless the caller is IN
        # it. Matched on address rather than guessed from a username, and a
        # loud failure when absent -- the alternative is silently assigning to
        # a handle that merely looks like the caller.
        email = (requester_email or '').strip().lower()
        if not email:
            raise ValueError("'me' needs a signed-in caller with an address")
        with connection.cursor() as c:
            c.execute('SELECT handle, display_name, email FROM app.cube_people '
                      'WHERE lower(email) = %s AND active', [email])
            row = c.fetchone()
        if row is None:
            raise ValueError("you have no entry in the people directory \u2014 add one "
                             "before assigning to yourself")
        return row[0], {'handle': row[0], 'display_name': row[1] or row[0], 'email': row[2]}
    with connection.cursor() as c:
        c.execute('SELECT handle, display_name, email, active FROM app.cube_people '
                  'WHERE lower(handle) = %s', [name])
        row = c.fetchone()
    if row is None:
        raise ValueError('no such handle %r \u2014 add it to the people directory first'
                         % name)
    if not row[3]:
        raise ValueError('handle %r is not active \u2014 assigning to a role nobody '
                         'holds is the same as assigning to nobody' % name)
    return row[0], {'handle': row[0], 'display_name': row[1] or row[0], 'email': row[2]}


def _current_assignee(subject_type, subject_key, author_key):
    """The assignee a row already carries, read only when one is being SET.

    Needed for the from/to on the trail. Not read on every post: an ordinary
    re-save that says nothing about assignment pays nothing for this.
    """
    with connection.cursor() as c:
        c.execute('SELECT assignee_role FROM app.cube_comments WHERE subject_type = %s '
                  'AND subject_key = %s AND author_key = %s',
                  [subject_type, subject_key, author_key])
        row = c.fetchone()
    return row[0] if row else ''


def _record_assignment(request, row, before, holder, actor_key):
    """Log a change of hands, and say on the activity trail that it happened.

    TWO writes, with two different jobs, deliberately -- not one fact recorded
    twice. `app.cube_comment_assignments` is the RECORD: typed columns, so
    "how long has this been with the bookkeeper" is an indexed query rather
    than a JSON dig, and it carries the holder snapshot the seat cannot keep.
    The activity event is the NOTICE: assignment shows up on the audit surface
    beside replies, and carries pointers rather than a second copy of the
    content -- exactly how the reply event carries reply_id and not the text.

    A no-op assignment writes nothing: re-saving a point that is already with
    the bookkeeper is not a change of hands, and logging it would make the
    ageing number reset every time somebody retypes the note.
    """
    to_role = holder.get('handle') or ''
    if before == to_role:
        return
    comment_id = row.get('id')
    if comment_id:
        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comment_assignments '
                '(comment_id, from_role, to_role, held_by, held_by_email, changed_by) '
                'VALUES (%s,%s,%s,%s,%s,%s)',
                [comment_id, before, to_role, holder.get('display_name') or '',
                 holder.get('email') or '', actor_key or ''])
    from apps.activity import models as A
    from apps.activity.services import record_activity
    record_activity(
        request, A.CUBE_COMMENT_ASSIGNED, target_kind='cube_comment',
        target_id=comment_id or '', target_ref=row.get('subject_label') or '',
        changes={'assignee_role': {'from': before, 'to': to_role}},
    )


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
    return _shape(dict(zip(COL_NAMES, r)))


def _shape(d):
    """The public shape of one register row, from a {column: value} mapping.

    Split out of _row_to_dict because the list query selects EXTRA columns
    (reply_count) that must not disturb the base shape -- the mapping is built
    from cursor.description there, and this stays the single definition of what
    a comment looks like on the wire.
    """
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
        'assignee_role': d['assignee_role'],
        'status': d['status'],
        'decision': d['decision'],
        'tags': list(d['tags'] or []),
        'created_at': d['created_at'].isoformat() if d['created_at'] else None,
        'updated_at': d['updated_at'].isoformat() if d['updated_at'] else None,
    }


MAX_LIMIT = 5000
DEFAULT_LIMIT = 500

# reply_count is a correlated subquery rather than a GROUP BY join: the register
# list is capped at MAX_LIMIT rows and the (comment_id, created_at) index makes
# each count an index-only scan, whereas the join would have to aggregate the
# whole reply table before the LIMIT could be applied.
REPLY_COUNT_SQL = ('(SELECT count(*) FROM app.cube_comment_replies r '
                   ' WHERE r.comment_id = app.cube_comments.id) AS reply_count')


def _truthy(raw):
    """Query-string flag. '1', 'true', 'yes', 'on' — anything else is off."""
    return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _like(raw):
    """A user's search term as a safe ILIKE pattern.

    The term is a bound parameter, so this is not about injection -- it is about
    a '%' typed into the search box matching the whole register and an '_'
    matching any character. Both are escaped, and the backslash first so the
    escape character itself cannot be smuggled in.
    """
    term = str(raw or '').replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
    return '%%%s%%' % term


def _build_where(params):
    """The filter half of the register query, as (where_fragments, args).

    Split out of ``list_comments`` so the COUNT and the PAGE are guaranteed to
    filter identically. Two copies of this WHERE is how a total ends up
    describing a different population from the rows beside it -- the exact
    failure a "121 of 121" footer exists to prevent.
    """
    where, args = [], []

    status_filter = (params.get('status') or 'open').strip()
    if status_filter != 'all':
        where.append('status = %s')
        args.append(status_filter)

    for param, col in (('subject_type', 'subject_type'), ('subject_key', 'subject_key'),
                       ('author', 'author_key'), ('tenant', 'tenant_id'),
                       ('measure', 'measure'), ('decision', 'decision'),
                       ('assignee', 'assignee_role'),
                       ('assignee_role', 'assignee_role')):
        val = (params.get(param) or '').strip()
        if val:
            where.append('%s = %%s' % col)
            args.append(val)

    # Both spellings, unioned: the pane sends ?tag=, the MCP sends ?tags=a,b, and
    # the cube endpoint historically accepted them together. Containment (@>)
    # means ALL of them -- && would WIDEN the queue, which is the opposite of
    # what someone filtering by tag wants.
    tags = _norm_tags(params.get('tags')) or []
    tags += [t for t in (_norm_tags(params.get('tag')) or []) if t not in tags]
    if tags:
        where.append('tags @> %s')
        args.append(tags)

    q = (params.get('q') or '').strip()
    if q:
        where.append("(comment ILIKE %s ESCAPE '\\' OR subject_label ILIKE %s ESCAPE '\\' "
                     "OR author ILIKE %s ESCAPE '\\')")
        args.extend([_like(q)] * 3)

    return where, args


# ── paging ────────────────────────────────────────────────────────────────
#
# The allowed page sizes are a CLOSED set, and an unknown one is a 400 rather
# than a silent clamp. A clamp is the dangerous shape here: ?page_size=2000
# quietly answered with 500 rows looks exactly like "there are only 500", and
# the caller has no way to tell the difference. Better to refuse and say what
# is allowed.
PAGE_SIZES = (50, 100, 200, 500, 1000)

# DEFAULT_PAGE_SIZE is 500 BECAUSE that is what DEFAULT_LIMIT has always been.
# The row shape of this list is a three-consumer contract -- /audit/cube-comments/
# (the auditor view), the Vue console and the Excel add-in all read it -- and a
# default that returns FEWER rows than a consumer expects is a silent
# truncation: the add-in would sync a partial register onto a sheet and nothing
# anywhere would say so. There are 121 rows today, so 500 changes nothing for
# anyone, and it is the one value in PAGE_SIZES that was already the default.
#
# `limit` / `offset` are left EXACTLY as they were and are NOT validated against
# PAGE_SIZES: the add-in sends limit=2000 today. Rejecting that would have been
# a loud break dressed up as a new feature. Paging is opt-in via `page_size`,
# and `total` is returned on every response either way, so a UI can render
# "121 of 121" without anyone changing how they fetch.
DEFAULT_PAGE_SIZE = 500


class PageSizeError(ValueError):
    """An unusable page_size. Carries the message the door returns verbatim."""


def _page_window(params):
    """(limit, offset, page, page_size) from either paging vocabulary.

    `page_size`/`page` is the new one and is strictly validated; `limit`/`offset`
    is the old one and keeps its old, forgiving behaviour. Sending both means
    page_size wins -- it is the explicit request.
    """
    raw_size = params.get('page_size')
    if raw_size not in (None, ''):
        try:
            page_size = int(raw_size)
        except (TypeError, ValueError):
            page_size = None
        if page_size not in PAGE_SIZES:
            raise PageSizeError(
                'page_size must be one of %s (got %r)'
                % (', '.join(str(s) for s in PAGE_SIZES), raw_size))
    else:
        page_size = None

    raw_page = params.get('page')
    if raw_page not in (None, ''):
        try:
            page = max(int(raw_page), 1)
        except (TypeError, ValueError):
            raise PageSizeError('page must be a positive whole number (got %r)' % raw_page)
        if page_size is None:
            page_size = DEFAULT_PAGE_SIZE
    else:
        page = 1

    if page_size is not None:
        return page_size, (page - 1) * page_size, page, page_size

    # Neither paging parameter given: the pre-existing limit/offset behaviour,
    # byte for byte.
    try:
        limit = min(max(int(params.get('limit', DEFAULT_LIMIT)), 1), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = max(int(params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return limit, offset, (offset // limit) + 1, limit


def count_comments(params):
    """How many rows match, ignoring the window. The 'of 121'."""
    _ensure_table()
    where, args = _build_where(params)
    sql = 'SELECT count(*) FROM app.cube_comments'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    with connection.cursor() as c:
        c.execute(sql, args)
        return int(c.fetchone()[0])


def page_comments(params, *, with_reply_counts=False, with_replies=False):
    """One page of the register, plus the total the page came out of.

    Returns the whole response body every door serves:
    ``{count, total, results, page, page_size, limit, offset, has_more}``.

    ``count`` keeps its original meaning -- how many rows are in THIS response --
    because three consumers already read it. ``total`` is the new key.
    """
    rows = list_comments(params, with_reply_counts=with_reply_counts,
                         with_replies=with_replies)
    limit, offset, page, page_size = _page_window(params)
    total = count_comments(params)
    return {
        'count': len(rows),
        'total': total,
        'results': rows,
        'page': page,
        'page_size': page_size,
        'limit': limit,
        'offset': offset,
        'has_more': offset + len(rows) < total,
        'page_sizes': list(PAGE_SIZES),
    }


def list_comments(params, *, with_reply_counts=False, with_replies=False):
    """The comment register list — SHARED by every door onto the register.

    One query, three doors: the console's generic list (/xero/data/comments/),
    the cube/add-in list (/xero/data/journals/pivot/comments/) and the auditor
    surface (/audit/cube-comments/). They must show the same rows with the same
    filters, and the only reliable way to guarantee that is for all of them to
    run the same SQL: two copies drift the first time a filter is added to one.

    Filters: status (default 'open', 'all' for every status), subject_type,
    subject_key, author (author_key), assignee (assignee_role), tenant (tenant_id),
    measure, decision,
    tag / tags (containment -- ALL of them, so a tag filter NARROWS), q (free
    text over the note, the subject label and the author), limit, offset --
    or page / page_size, see _page_window.

    ``with_reply_counts`` adds ``reply_count``; ``with_replies`` additionally
    inlines the thread itself (and implies the count).
    """
    _ensure_table()
    with_reply_counts = with_reply_counts or with_replies
    where, args = _build_where(params)
    limit, offset, _page, _page_size = _page_window(params)

    cols = COLS + (', ' + REPLY_COUNT_SQL if with_reply_counts else '')
    sql = 'SELECT %s FROM app.cube_comments' % cols
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    # id as a tie-break: updated_at alone is not unique (a bulk flag writes many
    # rows in one statement), and without it paging can repeat or skip a row.
    sql += ' ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s'
    args.extend([limit, offset])

    with connection.cursor() as c:
        c.execute(sql, args)
        names = [d[0] for d in c.description]
        rows = [dict(zip(names, r)) for r in c.fetchall()]

    out = []
    for d in rows:
        item = _shape(d)
        if with_reply_counts:
            item['reply_count'] = int(d.get('reply_count') or 0)
        out.append(item)

    if with_replies:
        from apps.xero.xero_data import cube_comment_replies as replies
        threads = replies.replies_for([row['id'] for row in out])
        for item in out:
            item['replies'] = threads.get(item['id'], [])
    return out


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
        # This endpoint is the CUBE's view of the register. Now that the same
        # table also holds bank transactions (and will hold more kinds), it has
        # to say so — otherwise the add-in fetches comments it can never place
        # on a sheet, and its counts describe a bigger queue than it shows.
        params = dict(request.query_params.items())
        params['subject_type'] = 'cube_cell'
        # ?include_replies=1 inlines the thread. Off by default: this list is
        # fetched with limit=2000 by the add-in and by the MCP tools, and most
        # comments have no replies at all — every caller paying for the join to
        # serve the few that do is the wrong default. The add-in asks for it.
        want_replies = _truthy(request.query_params.get('include_replies'))
        try:
            body = page_comments(params, with_reply_counts=True, with_replies=want_replies)
        except PageSizeError as exc:
            return Response({'error': str(exc), 'page_sizes': list(PAGE_SIZES)},
                            status=http.HTTP_400_BAD_REQUEST)
        return Response(body)

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

        # ABSENT tags mean "leave the stored ones alone", exactly as _norm_tags
        # documents and exactly as the generic door already behaved. This door
        # did `if tags is None: tags = []`, which BLANKED a user's tags on every
        # re-post that carried no `tags` key -- and the add-in re-posts a note
        # whenever the cell is re-saved. Same class of data loss as the receipts
        # bug in tests_archive.py ("a decision-less PATCH blanked the decision"):
        # a client that does not know a field exists must not be able to empty
        # it. Present-and-empty is still the explicit "clear my tags".
        tags = _norm_tags(d.get('tags'))
        tags_set = '  tags = EXCLUDED.tags, ' if tags is not None else ''
        if tags is None:
            tags = []          # the INSERT half; the column is NOT NULL

        assign_given = 'assignee' in d
        try:
            assignee, assignee_holder = _resolve_assignee(
                d.get('assignee'), requester_email=getattr(request.user, 'email', ''))
        except ValueError as exc:
            return Response({'error': str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        assignee_before = _current_assignee('cube_cell', key, author_key) if assign_given else ''
        # An OMITTED assignee must leave the stored one alone; present-and-empty
        # is the explicit unassign. Same data-loss guard receipts already carries
        # (tests_archive.py: "a decision-less PATCH blanked the decision") -- an
        # add-in re-sync that does not know about assignment must not silently
        # empty somebody's queue.
        assign_set = '  assignee_role = EXCLUDED.assignee_role, ' if assign_given else ''

        with connection.cursor() as c:
            c.execute(
                # subject_type/subject_key are the register's identity since the
                # generic-comment migration; cell_key is kept in step for the legacy
                # column and readers. ON CONFLICT must name the index that actually
                # exists (subject_type, subject_key, author_key) - _ensure_table drops
                # the old cube_comments_cell_author_uq, so conflicting on
                # (cell_key, author_key) raised ProgrammingError on every POST.
                'INSERT INTO app.cube_comments '
                '(cell_key, subject_type, subject_key, tenant_id, measure, row_dims, row_path, col_dims, col_path, '
                ' filters, cell_value, comment, author, author_key, assignee_role, status, tags) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (subject_type, subject_key, author_key) DO UPDATE SET '
                '  comment = EXCLUDED.comment, cell_value = EXCLUDED.cell_value, '
                '  author = EXCLUDED.author, status = EXCLUDED.status, ' +
                assign_set + tags_set +
                '  updated_at = now() '
                'RETURNING ' + COLS,
                [key, 'cube_cell', key, tenant, measure, list(row_dims), list(row_path), list(col_dims),
                 col_path, json.dumps(filters), val, comment,
                 author_name, author_key, assignee, (d.get('status') or 'open').strip(), tags],
            )
            row = c.fetchone()
        out = _row_to_dict(row)
        out['author_verified'] = verified
        if assign_given:
            _record_assignment(request, out, assignee_before, assignee_holder, author_key)

        # QUEUED, not sent. Notification is no longer a side effect of saving:
        # MC wrote 68 comments in a day, 33 inside one hour, and per-mention
        # email would have put 33 messages into one bookkeeper's inbox that
        # evening. The send is an explicit act -- see XeroCubeCommentNotifyView.
        out['mentions'] = cube_mentions.queue(
            out, cube_mentions.parse_mentions(comment))
        return Response(out, status=http.HTTP_200_OK)


class XeroCubeCommentIdentityView(APIView):
    """GET /xero/data/journals/pivot/comments/identity/

    "Who will this comment be signed as?" — asked BEFORE anything is written.

    The task pane no longer has a name box, so without this it could not say
    whose name is going on the note until after the note was saved. It reads
    the same _author_identity the POST paths use, so the pane cannot show one
    answer while the register records another.

    Safe method under /xero/data/journals/, so the service_readonly gate
    already allows it and no middleware change was needed to add it.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        author_key, author_name, verified = _author_identity(request, None)
        return Response({
            'author': author_name,
            'author_key': author_key,
            # False means "this caller has to declare its own name" — the MCP
            # service token. The pane never sees it; agents already do.
            'verified': verified,
            'stamped': verified,
        })


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


class XeroCubeCommentNotifyView(APIView):
    """GET/POST /xero/data/journals/pivot/comments/<id>/notify/

    "Who is waiting to be told about this?" and "tell them now."

    Split from the comment POST deliberately. An `@bookkeeper` in a note
    records an INTENT; nothing leaves the building until someone asks for it.
    That is MC's call and it is the right one -- his words were "I don't want
    to spam people", and the volume measurement backed him: 68 comments in a
    day, 33 in one hour, all resolving to one bookkeeper at Moore.

    GET is what the affordance reads ("1 person to notify"); POST is the act.
    `mention_ids` narrows it, so "her but not him" needs no second endpoint.

    Not reachable by an auditor (everything under /xero/data/ is 403 for that
    role) and not by the Excel add-in either: SERVICE_READONLY_POST_RE is an
    anchored allowlist naming comments/<id>/status and not this. Both are
    deliberate -- outbound mail on someone else's behalf is not a thing a
    read-only credential or an external auditor should be able to trigger.
    """
    permission_classes = [IsAuthenticated]

    def _load(self, comment_id):
        with connection.cursor() as c:
            c.execute('SELECT ' + COLS + ' FROM app.cube_comments WHERE id = %s',
                      [comment_id])
            row = c.fetchone()
        return _row_to_dict(row) if row else None

    def get(self, request, comment_id):
        _ensure_table()
        if self._load(comment_id) is None:
            return Response({'error': 'no such comment'}, status=http.HTTP_404_NOT_FOUND)
        pending = cube_mentions.pending_for(comment_id)
        return Response({'comment_id': comment_id, 'pending': pending,
                         'count': len(pending)})

    def post(self, request, comment_id):
        _ensure_table()
        row = self._load(comment_id)
        if row is None:
            return Response({'error': 'no such comment'}, status=http.HTTP_404_NOT_FOUND)
        ids = (request.data or {}).get('mention_ids')
        if ids is not None and not isinstance(ids, (list, tuple)):
            return Response({'error': 'mention_ids must be a list'},
                            status=http.HTTP_400_BAD_REQUEST)
        result = cube_mentions.send_pending(
            row,
            _coords(row['row_dims'], row['row_path'], row['col_dims'], row['col_path']),
            row.get('author') or '',
            only_ids=ids,
        )
        return Response({'comment_id': comment_id, **result})


# A captured context is read by a person deciding what to fix, not by a
# reconciliation. Past a couple of hundred lines it stops being evidence and
# starts being a database dump attached to every point -- and it is stored, so
# the cost is paid on every read forever rather than once.
MAX_CONTEXT_LINES = 200

MAX_COMMENT_TEXT = 20_000


def _may_edit(request, row, editor_key):
    """May this caller rewrite THIS comment's text?

    Two ways in, and no third:

    * an ADMIN (is_superuser or is_staff) may edit any note, including an
      agent-authored one -- correcting a wrong note in the register is exactly
      what MC needs to be able to do, and `author_key` is left untouched by the
      UPDATE, so the correction never re-attributes the words;
    * the AUTHOR may edit their own, matched on the SAME `author_key` the write
      doors stamp, so "my note" means here what it means everywhere else.

    Everyone else is refused. The endpoint originally checked nothing beyond
    IsAuthenticated, on the reasoning that the only account with the role that
    reaches /xero/data/ is MC's -- true today, and a property of the user table
    rather than of this code. The second standard account created (a bookkeeper
    with a console login is the obvious one) would have inherited the ability
    to silently rewrite MC's notes and every agent's, with the trail recording
    it as an ordinary edit. The check is added before the endpoint ships rather
    than after, which is the only cheap time to add it.

    Consciously accepted: the MCP service token resolves to `unattributed`
    unless the caller declares a name, and `author` is refused on this door --
    so an AGENT cannot yet edit its own note through here. It can still re-post
    through the upsert door, which is how it writes today. Widening this needs a
    way for a caller to prove an identity rather than assert one, and that is a
    bigger decision than an edit endpoint.
    """
    user = getattr(request, 'user', None)
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    return bool(editor_key) and editor_key == (row.get('author_key') or '')


class XeroCubeCommentTextView(APIView):
    """POST/GET /xero/data/journals/pivot/comments/<id>/text/  {comment}

    Edit what a point SAYS, with a record of what it said before.

    Id-addressed, and that is not a style choice. The register upserts on
    (subject, author) with the author stamped from the request, so "editing"
    through the POST door would not touch an agent's comment at all -- it would
    insert a SECOND row carrying that agent's text under the editor's name.
    Acting on someone else's row is only expressible by id.

    Text only. The anchor is identity: re-anchoring is a move, not an edit, and
    it changes which figure the note is about while looking like a typo fix.
    `author_key` is identity too -- an admin correcting an agent's note must
    never re-stamp it, or the register attributes words to a workstream that
    never wrote them. Neither is accepted here, so neither can be sent.

    Who may: the AUTHOR of a note, on their own note; and a superuser/staff on
    anyone's, including an agent's. Anyone else is 403 -- "role standard, which
    today is MC alone" is true today and is a property of the current user
    table, not of this endpoint; the second standard account added would have
    silently inherited the ability to rewrite MC's and every agent's notes.

    Two gates ABOVE this one still do the work they always did, and neither is
    touched: an auditor cannot reach any of /xero/data/ (so the one thing an
    auditor may write is still a reply), and the add-in's `service_readonly`
    identity is on an anchored POST allowlist that names comments/, bulk/,
    <id>/status/, subsets/ and views/ -- and not <id>/text/. Editing somebody
    else's words is not a read-only act.
    """
    permission_classes = [IsAuthenticated]

    def _row(self, comment_id):
        with connection.cursor() as c:
            c.execute('SELECT ' + COLS + ' FROM app.cube_comments WHERE id = %s', [comment_id])
            r = c.fetchone()
        return _row_to_dict(r) if r else None

    def get(self, request, comment_id):
        """The edit history, newest first. An empty list means never edited."""
        _ensure_table()
        if self._row(comment_id) is None:
            return Response({'error': 'no such comment'}, status=http.HTTP_404_NOT_FOUND)
        with connection.cursor() as c:
            c.execute('SELECT id, from_text, to_text, author_key, edited_by, edited_at '
                      'FROM app.cube_comment_edits WHERE comment_id = %s '
                      'ORDER BY edited_at DESC, id DESC', [comment_id])
            rows = c.fetchall()
        return Response({'comment_id': comment_id, 'count': len(rows), 'edits': [
            {'id': r[0], 'from': r[1], 'to': r[2], 'author_key': r[3],
             'edited_by': r[4], 'edited_at': r[5].isoformat() if r[5] else None}
            for r in rows]})

    def post(self, request, comment_id):
        _ensure_table()
        row = self._row(comment_id)
        if row is None:
            return Response({'error': 'no such comment'}, status=http.HTTP_404_NOT_FOUND)

        editor_key, _n, _v = _author_identity(request, None)
        if not _may_edit(request, row, editor_key):
            return Response(
                {'error': 'you can edit your own comments; editing someone else’s '
                          'needs an admin account'},
                status=http.HTTP_403_FORBIDDEN)

        d = request.data if isinstance(request.data, dict) else {}
        for forbidden in ('author', 'author_key', 'filters', 'row_dims', 'row_path',
                          'col_dims', 'col_path', 'measure', 'subject_key', 'cell_key'):
            if forbidden in d:
                # Refused rather than ignored. Silently dropping a field a caller
                # sent means they believe they changed something they did not,
                # and for the anchor that belief is the whole bug.
                return Response(
                    {'error': '%s cannot be edited \u2014 it is part of what identifies '
                              'this comment. Write a new comment instead.' % forbidden},
                    status=http.HTTP_400_BAD_REQUEST)

        text = str(d.get('comment') or '').strip()
        if not text:
            # NOT a retract. Retract-by-empty is coherent when re-saving a note
            # ON A CELL, where the anchor says which note you mean; on an
            # id-addressed edit it would delete a specific row that somebody
            # meant to correct.
            return Response({'error': 'comment must not be empty \u2014 an edit changes '
                                      'the text, it does not retract the point'},
                            status=http.HTTP_400_BAD_REQUEST)
        if '\x00' in text:
            return Response({'error': 'comment must not contain NUL (0x00) characters'},
                            status=http.HTTP_400_BAD_REQUEST)
        if len(text) > MAX_COMMENT_TEXT:
            return Response({'error': 'comment must be at most %d characters'
                                      % MAX_COMMENT_TEXT},
                            status=http.HTTP_400_BAD_REQUEST)

        before = row['comment']
        if text == before:
            # A no-op is not an edit. Logging it would fill the history with
            # rows that say nothing changed.
            return Response({**row, 'edited': False})

        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comment_edits '
                '(comment_id, from_text, to_text, author_key, edited_by) '
                'VALUES (%s,%s,%s,%s,%s)',
                [comment_id, before, text, row['author_key'], editor_key])
            # updated_at moves; author_key, subject_key, filters and cell_key
            # are not in this statement at all, so no edit can disturb them.
            c.execute('UPDATE app.cube_comments SET comment = %s, updated_at = now() '
                      'WHERE id = %s RETURNING ' + COLS, [text, comment_id])
            out = _row_to_dict(c.fetchone())

        from apps.activity import models as A
        from apps.activity.services import record_activity
        record_activity(
            request, A.CUBE_COMMENT_EDITED, target_kind='cube_comment',
            target_id=comment_id, target_ref=out.get('subject_label') or '',
            # Pointers and lengths, not a second copy of both texts -- the log
            # is the record, this is the notice, same split as assignment.
            changes={'chars': {'from': len(before), 'to': len(text)},
                     'author_key': row['author_key'],
                     'admin_edit': row['author_key'] != editor_key},
        )
        # Newly written text can mention someone who was not mentioned before.
        out['mentions'] = cube_mentions.queue(out, cube_mentions.parse_mentions(text))
        out['edited'] = True
        return Response(out)


class XeroCubeCommentContextView(APIView):
    """POST/GET /xero/data/journals/pivot/comments/<id>/context/

    Capture the lines behind a point, so the person fixing it can see what it
    is about without being given the general ledger.

    A separate act rather than part of saving a comment, for the same reason
    the mention send is: MC wrote 68 comments in one day and resolving a drill
    on every one of them would make writing a note cost a ledger scan. The
    points that go to a preparer are the few that need this.

    Re-capturing REPLACES, deliberately. This is a snapshot of one moment, not
    a history -- and unlike assignment, where every change of hands matters,
    nobody needs the third-most-recent view of the same lines. The moment that
    matters is recorded in `captured_at`.
    """
    permission_classes = [IsAuthenticated]

    def _comment(self, comment_id):
        with connection.cursor() as c:
            c.execute('SELECT ' + COLS + ' FROM app.cube_comments WHERE id = %s', [comment_id])
            row = c.fetchone()
        return _row_to_dict(row) if row else None

    def get(self, request, comment_id):
        _ensure_table()
        return Response(fetch_context(comment_id) or
                        {'comment_id': comment_id, 'captured_at': None, 'lines': []})

    def post(self, request, comment_id):
        _ensure_table()
        row = self._comment(comment_id)
        if row is None:
            return Response({'error': 'no such comment'}, status=http.HTTP_404_NOT_FOUND)
        if row['subject_type'] != 'cube_cell':
            # A bank transaction IS the transaction; there is nothing to drill
            # into. Saying so beats storing an empty capture that reads as "no
            # lines found" and looks like the ledger moved.
            return Response({'error': 'only a cube_cell comment has lines behind it; '
                                      'a %s comment already names its subject'
                                      % row['subject_type']},
                            status=http.HTTP_400_BAD_REQUEST)

        coords = _coords(row['row_dims'], row['row_path'], row['col_dims'], row['col_path'])
        params = {str(k): '' if v is None else str(v)
                  for k, v in (row['filters'] or {}).items()}
        params.update({'coords': json.dumps(coords),
                       'measure': row['measure'] or 'amount',
                       'limit': str(MAX_CONTEXT_LINES)})

        from apps.xero.xero_data.pivot_views import drill_result
        data, error = drill_result(request, params)
        if error:
            return Response({'error': error}, status=http.HTTP_400_BAD_REQUEST)

        lines = data.get('rows') or []
        receipts = sum(1 for l in lines if l.get('receipt_count'))
        author_key, _name, _v = _author_identity(request, None)
        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comment_context '
                '(comment_id, coords, measure, lines, line_count, line_total, truncated, '
                ' receipt_count, cell_value_at_capture, captured_by) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (comment_id) DO UPDATE SET '
                '  coords = EXCLUDED.coords, measure = EXCLUDED.measure, '
                '  lines = EXCLUDED.lines, line_count = EXCLUDED.line_count, '
                '  line_total = EXCLUDED.line_total, truncated = EXCLUDED.truncated, '
                '  receipt_count = EXCLUDED.receipt_count, '
                '  cell_value_at_capture = EXCLUDED.cell_value_at_capture, '
                '  captured_by = EXCLUDED.captured_by, captured_at = clock_timestamp()',
                [comment_id, json.dumps(coords), data.get('measure') or '',
                 json.dumps(lines), len(lines), data.get('line_total'),
                 bool(data.get('truncated')), receipts, row['cell_value'], author_key])
        return Response(fetch_context(comment_id), status=http.HTTP_200_OK)


def fetch_context(comment_id):
    """One captured context, or None. Shared by the cube door and the audit door."""
    _ensure_table()
    with connection.cursor() as c:
        c.execute('SELECT comment_id, coords, measure, lines, line_count, line_total, '
                  '       truncated, receipt_count, cell_value_at_capture, captured_by, '
                  '       captured_at '
                  'FROM app.cube_comment_context WHERE comment_id = %s', [comment_id])
        r = c.fetchone()
    if not r:
        return None
    return {
        'comment_id': r[0], 'coords': _jsonb(r[1]), 'measure': r[2],
        'lines': _jsonb(r[3]), 'line_count': r[4],
        'line_total': float(r[5]) if r[5] is not None else None,
        'truncated': r[6], 'receipt_count': r[7],
        'cell_value_at_capture': float(r[8]) if r[8] is not None else None,
        'captured_by': r[9], 'captured_at': r[10].isoformat() if r[10] else None,
    }


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
                # Absent means LEAVE AS IS, all the way down: no per-cell tags
                # and no shared tags is a bulk flag that says nothing about
                # tags, and it must not empty the tags of a cell somebody
                # already tagged by hand. `tags_given` is what drives that; the
                # `or []` remains for the INSERT half, where the column is NOT
                # NULL (a bulk flag that named no tags at all once raised
                # NotNullViolation -- the pane always sends a list, which is why
                # it took an MCP-shaped payload to find).
                cell_tags = _norm_tags(cell.get('tags'))
                tags_given = cell_tags is not None or shared_tags is not None
                tags = cell_tags or shared_tags or []
                tags_set = '  tags = EXCLUDED.tags, ' if tags_given else ''

                val = cell.get('cell_value')
                try:
                    val = float(val) if val is not None else None
                except (TypeError, ValueError):
                    val = None

                key = _cell_key(tenant, measure, row_dims, row_path,
                                col_dims, col_path, filters)
                c.execute(
                    # Same identity columns and the same conflict target as the
                    # single-comment path above. This clause said
                    # (cell_key, author_key) -- an index _ensure_table DROPS --
                    # and left subject_key at its '' default, so every bulk flag
                    # raised ProgrammingError ("no unique or exclusion
                    # constraint matching") and, had it not, would have keyed
                    # every cell in the selection to the same ('cube_cell', '')
                    # subject and collapsed them into one row.
                    'INSERT INTO app.cube_comments '
                    '(cell_key, subject_type, subject_key, tenant_id, measure, row_dims, row_path, '
                    ' col_dims, col_path, filters, cell_value, comment, author, author_key, status, tags) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                    'ON CONFLICT (subject_type, subject_key, author_key) DO UPDATE SET '
                    '  comment = EXCLUDED.comment, cell_value = EXCLUDED.cell_value, '
                    + tags_set +
                    '  status = EXCLUDED.status, updated_at = now() '
                    'RETURNING id',
                    [key, 'cube_cell', key, tenant, measure, list(row_dims), list(row_path),
                     list(col_dims), col_path, json.dumps(filters), val, comment,
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
        try:
            body = page_comments(
                dict(request.query_params.items()),
                with_replies=_truthy(request.query_params.get('include_replies')),
            )
        except PageSizeError as exc:
            return Response({'error': str(exc), 'page_sizes': list(PAGE_SIZES)},
                            status=http.HTTP_400_BAD_REQUEST)
        return Response(body)

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

        # Assignment is a property of the COMMENT, not of the console that
        # usually writes it. An agent raising a correction request from the
        # Excel pane goes through THIS door, and a request that cannot name who
        # should act lands in nobody's queue.
        assign_given = 'assignee' in d
        try:
            assignee, assignee_holder = _resolve_assignee(
                d.get('assignee'), requester_email=getattr(request.user, 'email', ''))
        except ValueError as exc:
            return Response({'error': str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        assignee_before = (_current_assignee(subject_type, subject_key, author_key)
                           if assign_given else '')
        assign_set = '  assignee_role = EXCLUDED.assignee_role, ' if assign_given else ''

        # `_norm_tags` returns None for ABSENT, documented as "leave as is" --
        # but this door passed that None straight into a NOT NULL column, so
        # every post here without a `tags` key was a 500. It is the door the MCP
        # and an Excel-side agent use, and a correction request that 500s is a
        # request nobody ever sees. Absent now means what the helper says it
        # means: [] on insert, stored tags untouched on conflict.
        tags = _norm_tags(d.get('tags'))
        tags_set = '  tags = EXCLUDED.tags, ' if tags is not None else ''
        if tags is None:
            tags = []

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
                ' comment, author, author_key, assignee_role, status, decision, tags) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (subject_type, subject_key, author_key) DO UPDATE SET '
                '  comment = EXCLUDED.comment, subject_label = EXCLUDED.subject_label, '
                '  cell_value = EXCLUDED.cell_value, '
                '  filters = EXCLUDED.filters, status = EXCLUDED.status, ' +
                tags_set + assign_set +
                '  decision = EXCLUDED.decision, updated_at = now() '
                'RETURNING ' + COLS,
                [
                    # cell_key stays UNIQUE-shaped for the legacy column; for a
                    # non-cube subject it is simply the subject key.
                    '%s:%s' % (subject_type, subject_key),
                    subject_type, subject_key, (d.get('subject_label') or '').strip()[:300],
                    (context.get('tenant') or '').strip(), (d.get('measure') or '').strip(),
                    [], [], [], '', json.dumps(context), val,
                    comment, author_name, author_key, assignee,
                    (d.get('status') or 'open').strip(), decision, tags,
                ],
            )
            row = c.fetchone()
        out = _row_to_dict(row)
        out['author_verified'] = verified
        if assign_given:
            _record_assignment(request, out, assignee_before, assignee_holder, author_key)
        return Response(out, status=http.HTTP_200_OK)
