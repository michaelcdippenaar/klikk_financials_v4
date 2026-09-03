"""Assigning a point BY ID.

The upsert doors accept `assignee` too, and for writing your own comment they
are right. This endpoint exists because reassigning SOMEONE ELSE'S point through
them silently forks the row -- see XeroCubeCommentAssignView's docstring. Every
test here that matters is really asserting "one row, not two".

Gate tests use a real Bearer token: force_authenticate resolves the caller
before DRF and walks straight past AuditorGateMiddleware, so a 403 asserted that
way proves nothing.
"""
import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.xero.xero_data import cube_mentions, pivot_comments

U = get_user_model()
URL = '/xero/data/journals/pivot/comments/%s/assign/'


class AssignByIdTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()

    def setUp(self):
        self.mc = U.objects.create_user(username='mc-assign', email='mc-assign@x.test',
                                        password='pass12345!')
        self.mc.is_superuser = True; self.mc.is_staff = True
        if hasattr(self.mc, 'role'):
            self.mc.role = 'standard'
        self.mc.save()
        with connection.cursor() as c:
            c.execute("INSERT INTO app.cube_people (handle, display_name, email, active) "
                      "VALUES ('bk-t','Book Keeper','bk-t@x.test',true),"
                      "       ('gone-t','Former','gone-t@x.test',false) "
                      "ON CONFLICT (handle) DO UPDATE SET active = EXCLUDED.active")
            # a point written by an AGENT -- the case the upsert door forks
            c.execute("INSERT INTO app.cube_comments "
                      "(cell_key, subject_type, subject_key, tenant_id, measure, row_dims, "
                      " row_path, col_dims, col_path, filters, comment, author, author_key, status, tags) "
                      "VALUES ('k-assign','cube_cell','k-assign','t','amount','{}','{}','{}','', "
                      "        '{}'::jsonb,'agent note','codex','codex:fy2026-bank-review','open','{}') "
                      "RETURNING id")
            self.cid = c.fetchone()[0]

    def tearDown(self):
        with connection.cursor() as c:
            c.execute("DELETE FROM app.cube_comments WHERE cell_key = 'k-assign'")
            c.execute("DELETE FROM app.cube_people WHERE handle IN ('bk-t','gone-t')")

    def bearer(self, u):
        return 'Bearer ' + str(RefreshToken.for_user(u).access_token)

    def post(self, body, user=None):
        return self.client.post(URL % self.cid, data=json.dumps(body),
                                content_type='application/json',
                                HTTP_AUTHORIZATION=self.bearer(user or self.mc))

    def rows(self):
        with connection.cursor() as c:
            c.execute("SELECT id, author_key, assignee_role, comment FROM app.cube_comments "
                      "WHERE cell_key = 'k-assign' ORDER BY id")
            return c.fetchall()

    # --- the whole point -------------------------------------------------
    def test_assigning_an_agents_point_updates_it_and_does_not_fork(self):
        r = self.post({'assignee': 'bk-t'})
        self.assertEqual(r.status_code, 200, r.content[:200])
        rows = self.rows()
        self.assertEqual(len(rows), 1, 'assignment forked the row instead of updating it')
        self.assertEqual(rows[0][2], 'bk-t')

    def test_author_key_is_never_rewritten(self):
        self.post({'assignee': 'bk-t'})
        self.assertEqual(self.rows()[0][1], 'codex:fy2026-bank-review')

    def test_comment_text_is_untouched(self):
        self.post({'assignee': 'bk-t'})
        self.assertEqual(self.rows()[0][3], 'agent note')

    # --- the trail --------------------------------------------------------
    def test_a_change_of_hands_is_logged(self):
        self.post({'assignee': 'bk-t'})
        with connection.cursor() as c:
            c.execute("SELECT count(*) FROM app.cube_comment_assignments WHERE comment_id = %s",
                      [self.cid])
            self.assertEqual(c.fetchone()[0], 1)

    def test_reassigning_to_the_same_seat_writes_no_trail_row(self):
        self.post({'assignee': 'bk-t'})
        r = self.post({'assignee': 'bk-t'})
        self.assertFalse(r.json()['reassigned'])
        with connection.cursor() as c:
            c.execute("SELECT count(*) FROM app.cube_comment_assignments WHERE comment_id = %s",
                      [self.cid])
            self.assertEqual(c.fetchone()[0], 1, 'a no-op reset the ageing clock')

    def test_empty_string_unassigns_and_is_logged(self):
        self.post({'assignee': 'bk-t'})
        r = self.post({'assignee': ''})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.rows()[0][2], '')

    # --- refusals ---------------------------------------------------------
    def test_unknown_handle_is_400_not_a_silent_drop(self):
        r = self.post({'assignee': 'nobody-here'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.rows()[0][2], '')

    def test_inactive_seat_is_refused(self):
        r = self.post({'assignee': 'gone-t'})
        self.assertEqual(r.status_code, 400)

    def test_omitted_assignee_is_400(self):
        self.assertEqual(self.post({}).status_code, 400)

    def test_unknown_comment_is_404(self):
        r = self.client.post(URL % 99999999, data=json.dumps({'assignee': 'bk-t'}),
                             content_type='application/json',
                             HTTP_AUTHORIZATION=self.bearer(self.mc))
        self.assertEqual(r.status_code, 404)

    def test_anonymous_is_rejected(self):
        r = self.client.post(URL % self.cid, data=json.dumps({'assignee': 'bk-t'}),
                             content_type='application/json')
        self.assertIn(r.status_code, (401, 403))

    def test_auditor_cannot_assign_but_the_credential_works(self):
        aud = U.objects.create_user(username='aud-assign', email='aud-assign@x.test',
                                    password='pass12345!')
        if hasattr(aud, 'role'):
            aud.role = 'auditor'
        aud.must_change_password = False
        aud.save()
        r = self.post({'assignee': 'bk-t'}, user=aud)
        self.assertEqual(r.status_code, 403)
        # positive control over the SAME credential: the refusal is the route,
        # not a broken header.
        ok = self.client.get('/audit/cube-comments/', HTTP_AUTHORIZATION=self.bearer(aud))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.rows()[0][2], '')

    def test_decision_column_stays_empty(self):
        self.post({'assignee': 'bk-t'})
        with connection.cursor() as c:
            c.execute("SELECT coalesce(decision,'') FROM app.cube_comments WHERE id = %s", [self.cid])
            self.assertEqual(c.fetchone()[0], '')
