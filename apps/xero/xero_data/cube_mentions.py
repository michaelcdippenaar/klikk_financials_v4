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

def resolve_mention(token):
    """Resolve one token to {'handle', 'email', 'display_name'} or None.

    Order: the people directory first, then Django users, then an explicit
    address. The directory wins so that a handle MC has deliberately curated is
    never shadowed by a coincidental Django username.
    """
    ensure_tables()
    with connection.cursor() as c:
        c.execute('SELECT handle, display_name, email FROM app.cube_people '
                  'WHERE lower(handle) = %s AND active', [token])
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


def notify(comment_row, coords, author, tokens):
    """Resolve and email each mention. NEVER raises.

    The contract that matters: the comment is already saved by the time this
    runs, and nothing in here may undo that. A dead SMTP server must cost an
    email, never a comment -- so every failure is caught, written to
    app.cube_comment_mentions, and returned to the caller instead of
    propagating.

    Returns {notified, already_notified, failed, unresolved}.
    """
    ensure_tables()
    result = {'notified': [], 'already_notified': [], 'failed': [], 'unresolved': []}
    comment_id = comment_row.get('id')
    if not comment_id or not tokens:
        return result

    for token in tokens:
        try:
            person = resolve_mention(token)
        except Exception as exc:                       # pragma: no cover - defensive
            logger.warning('mention resolve failed for %r: %s', token, exc)
            person = None

        if not person:
            # Reported, not dropped. The author gets to see that @finance went
            # nowhere and can add them to the directory or fix the spelling.
            result['unresolved'].append(token)
            continue

        email = (person.get('email') or '').strip()
        if not email:
            result['unresolved'].append(token)
            continue

        # Claim the (comment, person) slot first. If a delivered row already
        # exists we stop here -- editing a comment five times must not email
        # the same person five times.
        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_comment_mentions (comment_id, handle, email) '
                'VALUES (%s, %s, %s) '
                'ON CONFLICT (comment_id, lower(email)) DO UPDATE SET handle = EXCLUDED.handle '
                'RETURNING id, notified_at',
                [comment_id, person.get('handle') or '', email],
            )
            row_id, notified_at = c.fetchone()
        if notified_at is not None:
            result['already_notified'].append(email)
            continue

        subject = 'Klikk: %s mentioned you on %s' % (
            author or 'someone', comment_row.get('measure') or 'a figure')
        try:
            # An explicit connection with fail_silently=False so a broken
            # backend raises HERE, inside the guard, and gets recorded --
            # rather than being swallowed by Django and looking like success.
            EmailMessage(
                subject=subject,
                body=_body(comment_row, coords, author),
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                to=[email],
                connection=get_connection(fail_silently=False),
            ).send()
        except Exception as exc:
            detail = '%s: %s' % (type(exc).__name__, exc)
            logger.warning('mention email to %s failed: %s', email, detail)
            with connection.cursor() as c:
                c.execute('UPDATE app.cube_comment_mentions SET error = %s WHERE id = %s',
                          [detail[:500], row_id])
            result['failed'].append({'email': email, 'error': detail[:500]})
            continue

        with connection.cursor() as c:
            c.execute("UPDATE app.cube_comment_mentions "
                      "SET notified_at = now(), error = '' WHERE id = %s", [row_id])
        result['notified'].append(email)

    return result


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

        with connection.cursor() as c:
            c.execute(
                'INSERT INTO app.cube_people (handle, display_name, email, active) '
                'VALUES (%s,%s,%s,%s) '
                'ON CONFLICT (handle) DO UPDATE SET '
                '  display_name = EXCLUDED.display_name, email = EXCLUDED.email, '
                '  active = EXCLUDED.active, updated_at = now() '
                'RETURNING id, handle, display_name, email, active',
                [handle, display, email, active])
            r = c.fetchone()
        return Response({'id': r[0], 'handle': r[1], 'display_name': r[2],
                         'email': r[3], 'active': r[4]})
