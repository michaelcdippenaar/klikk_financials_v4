"""
@mentions on cube comments, the people they resolve to, and the emails sent.

Three concerns live here so pivot_comments.py stays about comments:

1. app.cube_people -- the mentionable-people directory. Django has two user
   rows (mc@tremly.com and the shared excel-addin login) but the people MC
   actually wants to mention are his bookkeeper and his auditors, who have no
   logins and should not be given one just to receive an email. So mentionable
   people are their own small directory, maintained deliberately through an
   endpoint. Addresses are never inferred, scraped from Xero/contacts/WhatsApp,
   or guessed from a handle.

2. Parsing @mentions out of comment text (see MENTION_RE for the syntax and
   the two false-positive classes it is built to refuse).

3. app.cube_comment_mentions -- what was notified, when, and what went wrong.
   A mention that quietly goes nowhere is worse than a loud failure, so every
   attempt leaves a row: notified_at set means delivered and it will never be
   sent again for that comment+person; error set with notified_at NULL means it
   failed, is visible, and a later re-post may retry it.

Sending is best-effort and MUST NOT be able to lose a comment. See notify().
"""
import json
import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage, get_connection
from django.db import connection
from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

DDL = """
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.cube_people (
    id           bigserial PRIMARY KEY,
    handle       text        NOT NULL UNIQUE,
    display_name text        NOT NULL DEFAULT '',
    email        text        NOT NULL,
    active       boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cube_people_active_idx ON app.cube_people (active);
-- HOW to reach this person, kept apart from WHO they are. MC's view is that
-- email is the wrong platform for this and expects Slack, Google Chat or
-- WhatsApp later, so the queue records who and what while the sender decides
-- how. Defaults to 'email' because that is the transport that exists; an
-- unimplemented channel is refused at send time rather than silently skipped.
ALTER TABLE app.cube_people ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'email';

CREATE TABLE IF NOT EXISTS app.cube_comment_mentions (
    id          bigserial PRIMARY KEY,
    comment_id  bigint      NOT NULL,
    handle      text        NOT NULL DEFAULT '',
    email       text        NOT NULL,
    notified_at timestamptz,
    error       text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- One row per comment+person: this is what makes "never notified twice" a
-- property of the schema rather than of the code path that happens to run.
CREATE UNIQUE INDEX IF NOT EXISTS cube_comment_mentions_uq
    ON app.cube_comment_mentions (comment_id, lower(email));
CREATE INDEX IF NOT EXISTS cube_comment_mentions_comment_idx
    ON app.cube_comment_mentions (comment_id);
"""

_ready = False


def ensure_tables():
    global _ready
    if _ready:
        return
    with connection.cursor() as c:
        c.execute(DDL)
    _ready = True


# --------------------------------------------------------------------------
# Mention syntax
# --------------------------------------------------------------------------
# A mention is an "@" that starts a token, followed by a handle or an email.
#
# The leading guard is a WHITELIST of what may precede the "@": start of
# string, whitespace, or an opening bracket / list punctuation. A blacklist was
# the obvious first try and is wrong -- it has to enumerate every character
# that could end a word, and the two cases that matter both slip through a
# careless one:
#
#   "email bob@example.com about this"  -> "@" follows "b", a word character,
#                                          so it is NOT a mention. An address
#                                          quoted in prose must never notify.
#   "ref INV-2024@KLIKK"                -> "@" follows "4". Xero references
#                                          contain "@"; none of them are people.
#
# Accepted forms:
#   @sarah                  bare handle -> resolved against app.cube_people,
#                                          then Django usernames
#   @sarah@example.com      explicit address
#   @<sarah@example.com>    explicit address, angle-bracketed for addresses
#                           containing characters the bare form would clip
#
# Trailing sentence punctuation is not part of a handle: "ask @sarah." mentions
# "sarah". Handles are matched case-insensitively and stored lowercase.
_EMAIL_TAIL = r'@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z]{2,})+'
MENTION_RE = re.compile(
    r'(?:^|(?<=[\s(\[{,;:]))@'
    r'(?:<(?P<bracketed>[^>\s]+)>'
    r'|(?P<token>[A-Za-z0-9][A-Za-z0-9._+-]*(?:' + _EMAIL_TAIL + r')?))'
)

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+' + _EMAIL_TAIL + r'$')

MAX_MENTIONS = 20


def parse_mentions(text):
    """Ordered, de-duplicated list of raw mention tokens in ``text``.

    Returns the tokens as written (minus the leading @ and any angle
    brackets), lowercased. Resolution happens separately -- parsing does not
    need the database and is therefore cheap to test exhaustively.
    """
    out, seen = [], set()
    for m in MENTION_RE.finditer(text or ''):
        tok = (m.group('bracketed') or m.group('token') or '').strip()
        # A handle does not end in sentence punctuation. Never strip inside an
        # address: "sarah@example.com." -> "sarah@example.com".
        tok = tok.rstrip('.,;:!?-_')
        if not tok:
            continue
        tok = tok.lower()
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= MAX_MENTIONS:
            break
    return out


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def resolve_mention(token, include_inactive=False):
    """Resolve one token to {'handle', 'email', 'display_name'} or None.

    ``include_inactive`` is for QUEUEING. A mention of a stood-down seat used to
    resolve to None and vanish -- silently, which is the exact failure the loud
    rules here exist to prevent. Queueing records the intent and shows it as
    un-sendable with a reason instead; whether it CAN go is decided at send
    time, so reactivating a seat makes what is already queued sendable without
    anyone re-typing it.

    Order: the people directory first, then Django users, then an explicit
    address. The directory wins so that a handle MC has deliberately curated is
    never shadowed by a coincidental Django username.
    """
    ensure_tables()
    with connection.cursor() as c:
        c.execute('SELECT handle, display_name, email FROM app.cube_people '
                  'WHERE lower(handle) = %s AND (active OR %s)',
                  [token, bool(include_inactive)])
        row = c.fetchone()
    if row:
        return {'handle': row[0], 'display_name': row[1] or row[0], 'email': row[2]}

    User = get_user_model()
    user = (User.objects.filter(username__iexact=token).first()
            or User.objects.filter(email__iexact=token).first())
    if user and (user.email or '').strip():
        name = (user.get_full_name() or '').strip() or user.username
        return {'handle': user.username, 'display_name': name, 'email': user.email.strip()}

    # An address written out in full is self-resolving: the author supplied the
    # destination, so there is nothing to look up. Anything else is unresolved
    # and must be REPORTED -- never silently dropped.
    if EMAIL_RE.match(token):
        return {'handle': '', 'display_name': token, 'email': token}
    return None


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------

def _body(comment_row, coords, author):
    """Plain text. No HTML, no images, no tracking -- a transactional note to a
    named colleague, containing enough to identify the figure without Excel."""
    lines = [
        '%s mentioned you in a comment on a Klikk figure.' % (author or 'Someone'),
        '',
        'Comment:',
        '  %s' % (comment_row.get('comment') or ''),
        '',
        'The figure this is about:',
        '  Measure: %s' % (comment_row.get('measure') or ''),
    ]
    if coords:
        for dim, val in coords.items():
            lines.append('  %s: %s' % (dim, val))
    else:
        lines.append('  (grand total -- no dimension coordinates)')

    val = comment_row.get('cell_value')
    lines.append('  Value: %s' % ('{:,.2f}'.format(val) if isinstance(val, (int, float)) else 'n/a'))

    raw_filters = comment_row.get('filters') or {}
    if isinstance(raw_filters, str):
        # Defence in depth: jsonb reaches us as a string on some paths.
        try:
            raw_filters = json.loads(raw_filters)
        except (TypeError, ValueError):
            raw_filters = {}
    if not isinstance(raw_filters, dict):
        raw_filters = {}
    filters = {k: v for k, v in raw_filters.items() if v not in (None, '', [])}
    lines += ['', 'Filter context that produced the number:']
    if filters:
        lines += ['  %s: %s' % (k, v) for k, v in sorted(filters.items())]
    else:
        lines.append('  (no filters -- the whole ledger)')

    lines += [
        '',
        'The same coordinates under different filters is a DIFFERENT figure, so',
        'reproduce the number from the context above rather than from memory.',
        '',
        '-- Klikk Financials. This is a one-off notification because you were',
        'named in this comment; it is not a mailing list.',
    ]
    return '\n'.join(lines)


def queue(comment_row, tokens):
    """Resolve each mention and RECORD the intent. Sends nothing. Never raises.

    Notification is not a side effect of saving a comment. MC wrote 68 comments
    in one day, 33 of them inside a single hour -- per-mention email would have
    put 33 messages into one bookkeeper's inbox that evening. A design that
    only works while the user keeps their own volume down is the wrong design,
    so the send moved behind an explicit action and this half only queues.

    Three things fall out of taking SMTP off the request path, and they are the
    reason this is better rather than merely quieter:

    * an SMTP outage can no longer present as the comments page hanging -- a
      symptom this console has already paid a day for;
    * work at 21:54 has no send time to get wrong, because there is no send;
    * a queued mention is REVIEWABLE. A typo in a point can be fixed before
      Anzelle ever sees it, which an immediate send makes impossible.

    Returns {queued, already_notified, already_queued, unresolved}.
    """
    ensure_tables()
    result = {'queued': [], 'already_notified': [], 'already_queued': [],
              'unresolved': []}
    comment_id = comment_row.get('id')
    if not comment_id or not tokens:
        return result

    for token in tokens:
        try:
            # include_inactive: a mention of a stood-down seat is RECORDED and
            # shown as un-sendable, not dropped. Dropping it is how an intent
            # disappears with nothing anywhere reporting it.
            person = resolve_mention(token, include_inactive=True)
        except Exception as exc:                       # pragma: no cover - defensive
            logger.warning('mention resolve failed for %r: %s', token, exc)
            person = None
        if not person:
            # Reported, not dropped: the author gets to see that @finance went
            # nowhere and can fix the spelling or add them to the directory.
            result['unresolved'].append(token)
            continue
        email = (person.get('email') or '').strip()
        if not email:
            result['unresolved'].append(token)
            continue

        # Claiming the (comment, person) slot IS the queue. The unique index
        # makes re-saving a comment idempotent: a delivered mention is never
        # re-sent and a pending one is never duplicated.
        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comment_mentions (comment_id, handle, email) '
                'VALUES (%s, %s, %s) '
                'ON CONFLICT (comment_id, lower(email)) DO UPDATE SET handle = EXCLUDED.handle '
                'RETURNING id, notified_at, (xmax = 0) AS inserted',
                [comment_id, person.get('handle') or '', email],
            )
            _row_id, notified_at, inserted = c.fetchone()
        if notified_at is not None:
            result['already_notified'].append(email)
        elif inserted:
            result['queued'].append(email)
        else:
            result['already_queued'].append(email)
    return result


def pending_for(comment_id):
    """Who is waiting to be told about this comment, and how they'd be reached."""
    ensure_tables()
    with connection.cursor() as c:
        c.execute(
            'SELECT m.id, m.handle, m.email, '
            "       COALESCE(p.channel, 'email'), COALESCE(p.display_name, m.handle), "
            '       p.handle IS NOT NULL, COALESCE(p.active, true), m.error '
            'FROM app.cube_comment_mentions m '
            'LEFT JOIN app.cube_people p ON lower(p.handle) = lower(m.handle) '
            'WHERE m.comment_id = %s AND m.notified_at IS NULL '
            'ORDER BY m.id', [comment_id])
        rows = c.fetchall()
    out = []
    for r in rows:
        channel, in_directory, active = r[3], r[5], r[6]
        # Sendability is decided HERE, at read time, never frozen onto the row.
        # A seat stood down today and brought back tomorrow must make what is
        # already queued sendable without anyone re-typing the comment.
        blocked = ''
        if in_directory and not active:
            blocked = 'the %r seat is stood down \u2014 reactivate it to send' % r[1]
        elif channel != 'email':
            blocked = '%r is not an implemented channel yet' % channel
        out.append({'id': r[0], 'handle': r[1], 'email': r[2], 'channel': channel,
                    'display_name': r[4] or r[1], 'sendable': not blocked,
                    'blocked_reason': blocked, 'last_error': r[7] or ''})
    return out


def _deliver(channel, recipient, subject, body):
    """The transport seam. One place that knows HOW, so the queue only knows WHO.

    An unknown channel RAISES rather than returning quietly. Adding
    `channel='whatsapp'` to a directory row must not start sending WhatsApp
    messages the moment someone edits the directory -- MC's standing rule is
    that no WhatsApp message goes out without his explicit confirmation of
    recipient AND text, and a seam that silently no-ops would make that rule
    unenforceable while looking like it worked. Failing loudly keeps the
    decision where it belongs: in whoever implements that channel.
    """
    if channel != 'email':
        raise NotImplementedError(
            '%r is not an implemented channel yet \u2014 mentions for %s stay queued'
            % (channel, recipient))
    EmailMessage(
        subject=subject,
        body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=[recipient],
        # fail_silently=False so a broken backend raises HERE, inside the
        # caller's guard, and is recorded -- rather than being swallowed by
        # Django and looking like a successful send.
        connection=get_connection(fail_silently=False),
    ).send()


def send_pending(comment_row, coords, author, only_ids=None):
    """Send the queued mentions for one comment. The EXPLICIT action. Never raises.

    Separate from `queue` on purpose: nothing reaches anybody until a person
    asks for it. `only_ids` narrows to a subset, so "notify her but not him"
    is expressible without inventing a second endpoint.

    Returns {notified, failed, skipped}.
    """
    ensure_tables()
    out = {'notified': [], 'failed': [], 'skipped': [], 'blocked': []}
    comment_id = comment_row.get('id')
    if not comment_id:
        return out
    wanted = None if only_ids is None else {int(i) for i in only_ids}
    subject = 'Klikk: %s mentioned you on %s' % (
        author or 'someone', comment_row.get('measure') or 'a figure')
    body = _body(comment_row, coords, author)

    for person in pending_for(comment_id):
        if wanted is not None and person['id'] not in wanted:
            out['skipped'].append(person['email'])
            continue
        if not person['sendable']:
            # Stays queued. A blocked mention is not a failed one -- it becomes
            # sendable the moment the seat is back, with no re-typing.
            out['blocked'].append({'email': person['email'],
                                   'reason': person['blocked_reason']})
            continue
        try:
            _deliver(person['channel'], person['email'], subject, body)
        except Exception as exc:
            detail = '%s: %s' % (type(exc).__name__, exc)
            logger.warning('mention to %s failed: %s', person['email'], detail)
            with connection.cursor() as c:
                c.execute('UPDATE app.cube_comment_mentions SET error = %s WHERE id = %s',
                          [detail[:500], person['id']])
            out['failed'].append({'email': person['email'], 'error': detail[:500]})
            continue
        with connection.cursor() as c:
            c.execute("UPDATE app.cube_comment_mentions "
                      "SET notified_at = now(), error = '' WHERE id = %s", [person['id']])
        out['notified'].append(person['email'])
    return out


HANDLE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


class XeroCubePeopleView(APIView):
    """
    GET  /xero/data/journals/pivot/people/?all=1
         The mentionable-people directory. Active only unless all=1.

    POST /xero/data/journals/pivot/people/
         {handle, email, display_name?, active?}   -- upsert on handle

    Deliberately a curated list rather than anything derived. The people MC
    mentions are his bookkeeper and auditors: they have no Django login, and
    their addresses must be entered on purpose, not inferred from a Xero
    contact or a WhatsApp thread that happens to contain an email.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ensure_tables()
        sql = ('SELECT id, handle, display_name, email, active, created_at, updated_at '
               'FROM app.cube_people')
        if (request.query_params.get('all') or '').strip() not in ('1', 'true', 'yes'):
            sql += ' WHERE active'
        sql += ' ORDER BY handle'
        with connection.cursor() as c:
            c.execute(sql)
            rows = c.fetchall()
        return Response({'count': len(rows), 'results': [{
            'id': r[0], 'handle': r[1], 'display_name': r[2], 'email': r[3],
            'active': r[4],
            'created_at': r[5].isoformat() if r[5] else None,
            'updated_at': r[6].isoformat() if r[6] else None,
        } for r in rows]})

    def post(self, request):
        ensure_tables()
        d = request.data or {}
        handle = (d.get('handle') or '').strip().lstrip('@').lower()
        email = (d.get('email') or '').strip()

        if not HANDLE_RE.match(handle or ''):
            return Response(
                {'error': 'handle must be 1-64 chars of letters, digits, dot, dash or '
                          'underscore, starting with a letter or digit'},
                status=http.HTTP_400_BAD_REQUEST)
        if not EMAIL_RE.match(email):
            # Refuse rather than store something that will fail at send time and
            # look like a mail problem instead of a data problem.
            return Response({'error': 'a valid email address is required'},
                            status=http.HTTP_400_BAD_REQUEST)

        active = d.get('active')
        active = True if active is None else bool(active)
        display = (d.get('display_name') or '').strip() or handle
        # Stored as given rather than validated against a list: a channel this
        # build cannot send on is refused at SEND time, loudly, which keeps an
        # unimplemented transport from looking configured and silently dropping
        # everything routed to it.
        channel = (d.get('channel') or 'email').strip().lower()

        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_people (handle, display_name, email, active, channel) '
                'VALUES (%s,%s,%s,%s,%s) '
                'ON CONFLICT (handle) DO UPDATE SET '
                '  display_name = EXCLUDED.display_name, email = EXCLUDED.email, '
                '  active = EXCLUDED.active, channel = EXCLUDED.channel, updated_at = now() '
                'RETURNING id, handle, display_name, email, active, channel',
                [handle, display, email, active, channel])
            r = c.fetchone()
        return Response({'id': r[0], 'handle': r[1], 'display_name': r[2],
                         'email': r[3], 'active': r[4], 'channel': r[5]})
