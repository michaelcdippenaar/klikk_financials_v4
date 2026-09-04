"""A re-post that says nothing about `status` must not reset it.

The same rule `_norm_tags` has always documented and two of three doors ignored,
now applied to the workflow flag. The failure it prevents is concrete: MC marks
a point `actioned` in the console, the add-in's next Build re-saves that sheet
note without a status -- because the pane does not know the field exists -- and
the point is `open` again. 17 of the 121 live rows are non-open, so that is the
size of what a single re-save could have undone.

`decision` carries the same guard. It is empty on every row today, so these
tests pin a property rather than fix a live loss -- which is the point: the door
an agent posts through is where a verdict would be cleared by a caller that
never sent one, on the day the first verdict is set.
"""
import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.xero.xero_data import cube_mentions, pivot_comments

U = get_user_model()
CUBE = '/xero/data/journals/pivot/comments/'
BULK = '/xero/data/journals/pivot/comments/bulk/'
GENERIC = '/xero/data/comments/'


class StatusSurvivesARepostTests(TestCase):
    def setUp(self):
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        self.mc = U.objects.create_user(username='mc-status', email='mc-status@x.test',
                                        password='pass12345!')
        self.mc.is_superuser = True
        self.mc.is_staff = True
        self.mc.must_change_password = False
        if hasattr(self.mc, 'role'):
            self.mc.role = 'standard'
        self.mc.save()

    def tearDown(self):
        with connection.cursor() as c:
            c.execute("DELETE FROM app.cube_comments WHERE tenant_id = 'status-t'")
            c.execute("DELETE FROM app.cube_comments WHERE subject_key LIKE 'bank-status-%'")

    def bearer(self):
        return 'Bearer ' + str(RefreshToken.for_user(self.mc).access_token)

    def post(self, url, body):
        r = self.client.post(url, data=json.dumps(body), content_type='application/json',
                             HTTP_AUTHORIZATION=self.bearer())
        # Assert here rather than at each call site: a 400 that silently stores
        # nothing looks identical to "the guard cleared the row", which is the
        # exact confusion that cost a debugging round on this file.
        assert r.status_code == 200, '%s -> %s %s' % (url, r.status_code, r.content[:200])
        return r

    def cube_body(self, **over):
        body = {'measure': 'amount', 'row_dims': ['account'], 'row_path': ['PM--MC07'],
                'col_dims': [], 'col_path': 'Total',
                'filters': {'tenant': 'status-t'}, 'comment': 'a note', 'cell_value': 1.0}
        body.update(over)
        return body

    def stored(self, tenant='status-t'):
        with connection.cursor() as c:
            c.execute("SELECT status, coalesce(decision,'') FROM app.cube_comments "
                      "WHERE tenant_id = %s ORDER BY id DESC LIMIT 1", [tenant])
            return c.fetchone()

    # --- the cube door: the one the Excel pane uses ------------------------
    def test_cube_repost_without_status_keeps_actioned(self):
        self.assertEqual(self.post(CUBE, self.cube_body(status='actioned')).status_code, 200)
        self.assertEqual(self.stored()[0], 'actioned')
        # the pane re-saving the sheet: same note, no status key at all
        self.assertEqual(self.post(CUBE, self.cube_body()).status_code, 200)
        self.assertEqual(self.stored()[0], 'actioned', 'a re-save reopened a closed point')

    def test_cube_new_comment_still_defaults_to_open(self):
        self.post(CUBE, self.cube_body())
        self.assertEqual(self.stored()[0], 'open')

    def test_cube_explicit_status_still_wins(self):
        self.post(CUBE, self.cube_body(status='actioned'))
        self.post(CUBE, self.cube_body(status='dismissed'))
        self.assertEqual(self.stored()[0], 'dismissed')

    def test_cube_repost_preserves_status_AND_tags_together(self):
        self.post(CUBE, self.cube_body(status='dismissed', tags=['vat']))
        self.post(CUBE, self.cube_body())
        st, _ = self.stored()
        with connection.cursor() as c:
            c.execute("SELECT tags FROM app.cube_comments WHERE tenant_id='status-t' "
                      "ORDER BY id DESC LIMIT 1")
            tags = c.fetchone()[0]
        self.assertEqual(st, 'dismissed')
        self.assertEqual(list(tags), ['vat'])

    # --- the bulk door ----------------------------------------------------
    def test_bulk_repost_without_status_keeps_actioned(self):
        cell = {'measure': 'amount', 'row_dims': ['account'], 'row_path': ['PM--MC07'],
                'col_dims': [], 'col_path': 'Total',
                'filters': {'tenant': 'status-t'}, 'comment': 'bulk note', 'cell_value': 2.0}
        self.assertEqual(self.post(BULK, {'cells': [dict(cell, status='actioned')]}).status_code, 200)
        self.assertEqual(self.stored()[0], 'actioned')
        self.assertEqual(self.post(BULK, {'cells': [dict(cell)]}).status_code, 200)
        self.assertEqual(self.stored()[0], 'actioned', 'a bulk re-save reopened a closed point')

    def test_bulk_shared_status_still_applies(self):
        cell = {'measure': 'amount', 'row_dims': ['account'], 'row_path': ['PM--MC07'],
                'col_dims': [], 'col_path': 'Total',
                'filters': {'tenant': 'status-t'}, 'comment': 'bulk note', 'cell_value': 2.0}
        self.post(BULK, {'cells': [cell]})
        self.post(BULK, {'status': 'dismissed', 'cells': [cell]})
        self.assertEqual(self.stored()[0], 'dismissed')

    # --- the generic door: status AND decision ----------------------------
    def generic_body(self, **over):
        body = {'subject_type': 'bank_txn', 'subject_key': 'bank-status-1',
                'subject_label': 'a payment', 'comment': 'check this'}
        body.update(over)
        return body

    def bank_stored(self):
        with connection.cursor() as c:
            c.execute("SELECT status, coalesce(decision,'') FROM app.cube_comments "
                      "WHERE subject_key = 'bank-status-1'")
            return c.fetchone()

    def test_generic_repost_without_status_keeps_it(self):
        self.assertEqual(self.post(GENERIC, self.generic_body(status='actioned')).status_code, 200)
        self.assertEqual(self.bank_stored()[0], 'actioned')
        self.post(GENERIC, self.generic_body())
        self.assertEqual(self.bank_stored()[0], 'actioned')

    def test_generic_repost_without_decision_keeps_the_verdict(self):
        self.post(GENERIC, self.generic_body(decision='personal'))
        self.assertEqual(self.bank_stored()[1], 'personal')
        self.post(GENERIC, self.generic_body())
        self.assertEqual(self.bank_stored()[1], 'personal',
                         'a decision-less re-post blanked the verdict')

    def test_generic_explicit_empty_decision_still_clears(self):
        self.post(GENERIC, self.generic_body(decision='personal'))
        self.post(GENERIC, self.generic_body(decision=''))
        self.assertEqual(self.bank_stored()[1], '')
