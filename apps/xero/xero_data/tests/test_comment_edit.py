"""
EDITING a point, and the ways an edit quietly becomes something else.

The endpoint (POST/GET /xero/data/journals/pivot/comments/<id>/text/) shipped
smoke-verified by hand and printed rather than asserted. Every property below is
one where a plausible implementation is wrong in a way nothing reports:

* an edit addressed by ANCHOR instead of by id does not edit anything -- the
  upsert doors key on (subject, author) and stamp the REQUESTER as author, so
  "correcting" an agent's note there inserts a SECOND row carrying that agent's
  words under the editor's name;
* an edit that re-stamps `author_key` makes the register attribute to
  `claude:year-end-audit` words a human wrote;
* an edit that moves the anchor changes WHICH FIGURE the note is about while
  looking like a typo fix;
* an edit with no record leaves the trail saying something other than what an
  agent acted on;
* a no-op logged as an edit fills the history with rows that say nothing;
* an endpoint gated only by IsAuthenticated lets the next console account
  rewrite MC's notes and every agent's.

GATE TESTS USE A REAL BEARER TOKEN, NEVER force_authenticate.
``force_authenticate`` sets ``request.user`` inside DRF, which runs AFTER
AuditorGateMiddleware -- the gate resolves nobody and passes the request
through, so a 403 test written that way asserts nothing at all. The role gates
are only observable over a credential the middleware can actually see.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.activity.models import ActivityEvent
from apps.xero.xero_data import cube_mentions, pivot_comments

User = get_user_model()

CUBE = '/xero/data/journals/pivot/comments/'
TEXT = '/xero/data/journals/pivot/comments/%s/text/'


def _payload(**over):
    body = {
        'measure': 'amount',
        'row_dims': ['account_class', 'account'],
        'row_path': ['EXPENSE', '406 - Consulting'],
        'col_dims': ['fin_year'],
        'col_path': 'FY2023',
        'filters': {'tenant': '', 'journal_type': ''},
        'cell_value': 134200,
        'comment': 'This looks wrong.',
    }
    body.update(over)
    return body


def _edits(comment_id=None):
    """The edit log, oldest first: (comment_id, from, to, author_key, edited_by)."""
    sql = ('SELECT comment_id, from_text, to_text, author_key, edited_by '
           'FROM app.cube_comment_edits')
    args = []
    if comment_id is not None:
        sql += ' WHERE comment_id = %s'
        args.append(comment_id)
    with connection.cursor() as c:
        c.execute(sql + ' ORDER BY id', args)
        return c.fetchall()


def _stored(comment_id):
    """(comment, author_key, subject_key, cell_key, filters) straight from the table."""
    with connection.cursor() as c:
        c.execute('SELECT comment, author_key, subject_key, cell_key, filters, decision '
                  'FROM app.cube_comments WHERE id = %s', [comment_id])
        return c.fetchone()


def _agent_row(author_key='claude:year-end-audit', text='Agent raised this.'):
    """A comment authored by something that is not the caller.

    Written straight to the table rather than through the POST door on purpose:
    the door stamps the REQUESTER as author, which is precisely the property
    that makes an id-addressed edit endpoint necessary in the first place.
    """
    with connection.cursor() as c:
        c.execute(
            'INSERT INTO app.cube_comments '
            '(cell_key, subject_type, subject_key, tenant_id, measure, row_dims, row_path, '
            ' col_dims, col_path, filters, comment, author, author_key, status, tags) '
            "VALUES (%s,'cube_cell',%s,'','amount','{}','{}','{}','','{}'::jsonb,"
            " %s,%s,%s,'open','{}') RETURNING id",
            ['agent-cell', 'agent-cell', text, author_key, author_key])
        return c.fetchone()[0]


class _Base(TestCase):
    """These modules create their tables lazily and cache 'done' in a module
    global. TestCase rolls each test back, DDL included, so the cache outlives
    the schema it describes and every test after the first sees "relation
    app.cube_comments does not exist". Reset both flags per test."""

    def setUp(self):
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        self.mc = User.objects.create_user(
            username='mc', email='mc@tremly.com', password='pw-not-logged',
            is_superuser=True, is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.mc)

    def _write(self, **over):
        r = self.client.post(CUBE, _payload(**over), format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data['id']

    def _bearer(self, user):
        """A client carrying a credential AuditorGateMiddleware can actually see."""
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION='Bearer %s' % RefreshToken.for_user(user).access_token)
        return client


# ------------------------------------------------------------ the edit ----

class EditTests(_Base):
    def test_edit_replaces_the_text_and_returns_the_new_row(self):
        cid = self._write(comment='This looks wrong.')
        r = self.client.post(TEXT % cid, {'comment': 'This is coded to 406, should be 6420.'},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['comment'], 'This is coded to 406, should be 6420.')
        self.assertTrue(r.data['edited'])
        self.assertEqual(_stored(cid)[0], 'This is coded to 406, should be 6420.')

    def test_history_carries_the_previous_text(self):
        cid = self._write(comment='first')
        self.client.post(TEXT % cid, {'comment': 'second'}, format='json')
        self.client.post(TEXT % cid, {'comment': 'third'}, format='json')

        r = self.client.get(TEXT % cid)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['count'], 2)
        # Newest first, and each row reads on its own without replaying the chain.
        self.assertEqual([(e['from'], e['to']) for e in r.data['edits']],
                         [('second', 'third'), ('first', 'second')])
        self.assertEqual({e['edited_by'] for e in r.data['edits']}, {'mc'})

    def test_history_of_an_unedited_comment_is_empty_not_404(self):
        cid = self._write()
        r = self.client.get(TEXT % cid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['count'], 0)
        self.assertEqual(r.data['edits'], [])

    def test_unknown_comment_is_404_on_both_verbs(self):
        self.assertEqual(self.client.get(TEXT % 987654).status_code, 404)
        self.assertEqual(
            self.client.post(TEXT % 987654, {'comment': 'x'}, format='json').status_code, 404)

    def test_an_edit_records_an_activity_event(self):
        cid = self._write(comment='short')
        self.client.post(TEXT % cid, {'comment': 'a considerably longer note'},
                         format='json')
        ev = ActivityEvent.objects.filter(action='cube_comment.edited').get()
        self.assertEqual(ev.target_id, str(cid))
        self.assertEqual(ev.changes['chars'], {'from': 5, 'to': 26})
        self.assertFalse(ev.changes['admin_edit'])


# --------------------------------------------------------- what is fixed ----

class ImmutabilityTests(_Base):
    def test_author_key_is_unchanged_when_an_admin_edits_an_agents_comment(self):
        cid = _agent_row()
        r = self.client.post(TEXT % cid, {'comment': 'Corrected by MC.'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['comment'], 'Corrected by MC.')
        # The whole point: MC's correction must not make the register say the
        # agent wrote MC's words.
        self.assertEqual(r.data['author_key'], 'claude:year-end-audit')
        self.assertEqual(_stored(cid)[1], 'claude:year-end-audit')
        # ...and the log says who actually changed it, beside who wrote it.
        self.assertEqual(_edits(cid), [(cid, 'Agent raised this.', 'Corrected by MC.',
                                        'claude:year-end-audit', 'mc')])

    def test_an_admin_edit_is_marked_as_such_on_the_trail(self):
        cid = _agent_row()
        self.client.post(TEXT % cid, {'comment': 'Corrected by MC.'}, format='json')
        ev = ActivityEvent.objects.filter(action='cube_comment.edited').get()
        self.assertTrue(ev.changes['admin_edit'])
        self.assertEqual(ev.changes['author_key'], 'claude:year-end-audit')

    def test_the_anchor_and_the_author_are_refused_not_ignored(self):
        cid = self._write()
        before = _stored(cid)
        for field, value in (('author', 'someone-else'),
                             ('author_key', 'someone-else'),
                             ('subject_key', 'other-cell'),
                             ('cell_key', 'other-cell'),
                             ('filters', {'tenant': 'other'}),
                             ('row_path', ['REVENUE', '200 - Sales']),
                             ('row_dims', ['account']),
                             ('col_dims', ['month']),
                             ('col_path', 'FY2024'),
                             ('measure', 'debit')):
            r = self.client.post(TEXT % cid, {'comment': 'moved', field: value},
                                 format='json')
            self.assertEqual(r.status_code, 400, '%s was accepted: %s' % (field, r.data))
            self.assertIn(field, r.data['error'])
        # Refused means nothing moved, including the text that came with it.
        self.assertEqual(_stored(cid), before)
        self.assertEqual(_edits(cid), [])

    def test_the_decision_column_is_never_touched(self):
        # Its vocabulary is an open decision MC has reserved; every row must
        # stay empty, and an edit must not be the thing that fills one in.
        cid = self._write()
        self.client.post(TEXT % cid, {'comment': 'changed', 'decision': 'personal'},
                         format='json')
        self.assertEqual(_stored(cid)[5], '')


class BadInputTests(_Base):
    def test_empty_text_is_a_400_and_not_a_retract(self):
        cid = self._write(comment='still relevant')
        for empty in ('', '   ', None):
            r = self.client.post(TEXT % cid, {'comment': empty}, format='json')
            self.assertEqual(r.status_code, 400, r.data)
        # The comment is still there. An id-addressed empty must never delete.
        self.assertEqual(_stored(cid)[0], 'still relevant')
        self.assertEqual(_edits(cid), [])

    def test_a_missing_comment_key_is_a_400(self):
        cid = self._write()
        self.assertEqual(self.client.post(TEXT % cid, {}, format='json').status_code, 400)

    def test_a_nul_byte_is_a_400_rather_than_a_500(self):
        cid = self._write()
        r = self.client.post(TEXT % cid, {'comment': 'bad\x00text'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)

    def test_text_over_the_cap_is_a_400(self):
        cid = self._write()
        r = self.client.post(TEXT % cid, {'comment': 'x' * 20_001}, format='json')
        self.assertEqual(r.status_code, 400, r.data)

    def test_a_no_op_reports_edited_false_and_writes_no_history(self):
        cid = self._write(comment='unchanged')
        r = self.client.post(TEXT % cid, {'comment': 'unchanged'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data['edited'])
        self.assertEqual(_edits(cid), [])
        self.assertFalse(ActivityEvent.objects.filter(action='cube_comment.edited').exists())

    def test_whitespace_only_difference_is_still_a_no_op(self):
        cid = self._write(comment='unchanged')
        r = self.client.post(TEXT % cid, {'comment': '  unchanged  '}, format='json')
        self.assertFalse(r.data['edited'])
        self.assertEqual(_edits(cid), [])


# ------------------------------------------------------------- who may ----

class WhoMayEditTests(_Base):
    """The gate that was missing.

    The endpoint shipped with IsAuthenticated alone, on the reasoning that the
    only role reaching /xero/data/ is `standard` and that today means MC. That
    is a property of the user table, not of this code: the second console
    account -- a bookkeeper is the obvious one -- inherits the ability to
    rewrite MC's notes and every agent's, and the trail records it as an
    ordinary edit.
    """

    def test_the_author_may_edit_their_own(self):
        anzelle = User.objects.create_user(username='anzelle', password='pw-not-logged')
        client = self._bearer(anzelle)
        cid = client.post(CUBE, _payload(comment='hers'), format='json').data['id']
        r = client.post(TEXT % cid, {'comment': 'hers, corrected'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(_stored(cid)[0], 'hers, corrected')

    def test_a_non_author_without_admin_is_403(self):
        anzelle = User.objects.create_user(username='anzelle', password='pw-not-logged')
        cid = self._write(comment='MC wrote this')          # authored by mc
        r = self._bearer(anzelle).post(TEXT % cid, {'comment': 'rewritten'}, format='json')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(_stored(cid)[0], 'MC wrote this')
        self.assertEqual(_edits(cid), [])

    def test_a_non_author_cannot_edit_an_agents_comment_either(self):
        anzelle = User.objects.create_user(username='anzelle', password='pw-not-logged')
        cid = _agent_row()
        r = self._bearer(anzelle).post(TEXT % cid, {'comment': 'rewritten'}, format='json')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(_stored(cid)[0], 'Agent raised this.')

    def test_a_superuser_may_edit_anyones(self):
        anzelle = User.objects.create_user(username='anzelle', password='pw-not-logged')
        cid = self._bearer(anzelle).post(
            CUBE, _payload(comment='hers'), format='json').data['id']
        r = self._bearer(self.mc).post(TEXT % cid, {'comment': 'fixed by MC'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(_stored(cid)[0], 'fixed by MC')
        self.assertEqual(_stored(cid)[1], 'anzelle')   # authorship still hers


class RoleGateTests(_Base):
    """The two RESTRICTED roles, over credentials the middleware can see.

    Both are enforced by AuditorGateMiddleware and NOT by this view, which is
    why they are asserted over a real Bearer / Token header: force_authenticate
    resolves the caller inside DRF, after the gate has already let the request
    past, so the same assertions written that way would pass against no gate at
    all.
    """

    def test_an_auditor_cannot_reach_the_edit_endpoint(self):
        auditor = User.objects.create_user(
            username='auditor@moore.example', password='pw-not-logged',
            role=User.Role.AUDITOR)
        cid = self._write(comment='MC wrote this')
        client = self._bearer(auditor)
        # Everything under /xero/data/ is closed to this role -- the one write
        # an auditor may make is a reply, under /audit/.
        self.assertEqual(
            client.post(TEXT % cid, {'comment': 'rewritten'}, format='json').status_code, 403)
        self.assertEqual(client.get(TEXT % cid).status_code, 403)
        self.assertEqual(_stored(cid)[0], 'MC wrote this')

    def test_the_gate_is_real_and_not_an_artefact_of_the_test_client(self):
        # Guards the guard: the same request from a standard account over the
        # same credential shape succeeds, so the 403 above is the ROLE and not
        # a broken Bearer header.
        cid = self._write(comment='MC wrote this')
        self.assertEqual(
            self._bearer(self.mc).post(TEXT % cid, {'comment': 'ok'},
                                       format='json').status_code, 200)

    def test_the_excel_add_in_identity_cannot_reach_the_edit_endpoint(self):
        addin = User.objects.create_user(
            username='excel-addin', password='pw-not-logged',
            role=User.Role.SERVICE_READONLY)
        token = Token.objects.create(user=addin)
        cid = self._write(comment='MC wrote this')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Token %s' % token.key)
        # SERVICE_READONLY_POST_RE is an anchored allowlist naming comments/,
        # comments/bulk/, comments/<id>/status/, subsets/ and views/. It does
        # not name <id>/text/, and editing someone else's words is not a
        # read-only act.
        r = client.post(TEXT % cid, {'comment': 'rewritten'}, format='json')
        # A middleware 403 is a plain JsonResponse, not a DRF Response.
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(_stored(cid)[0], 'MC wrote this')

    def test_the_add_in_can_still_post_a_comment(self):
        # Guards the guard again: the 403 above must be the ROUTE, not the
        # credential. The add-in's own writes keep working.
        addin = User.objects.create_user(
            username='excel-addin', password='pw-not-logged',
            role=User.Role.SERVICE_READONLY)
        token = Token.objects.create(user=addin)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Token %s' % token.key)
        self.assertEqual(
            client.post(CUBE, _payload(), format='json').status_code, 200)
