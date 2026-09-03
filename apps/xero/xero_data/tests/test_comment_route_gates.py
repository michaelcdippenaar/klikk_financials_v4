"""One table over every comment route the night of 2026-09-03 added, against
every credential that is NOT a named human.

Written after a CTO audit found that `/notify/` -- the one route that sends mail
OUT OF THE BUILDING, to a bookkeeper and an audit partner at an accounting firm
-- had no gate test at all. Its docstring asserted both restricted roles were
shut out; nothing proved it, and the shared MCP service token could in fact
reach it. "No MCP tool calls this endpoint" was the only thing standing between
a script and Anzelle's inbox, and absence of a caller is not a control.

Table-driven ON PURPOSE: the previous shape was one test per route, so a route
added later inherited no coverage by default. Here a new route is one row.

Every credential is a REAL header. `force_authenticate` resolves the caller
before DRF and never runs AuditorGateMiddleware, so a 403 asserted that way
proves nothing -- which is exactly how the notify gap survived review.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework_simplejwt.tokens import RefreshToken

from apps.xero.xero_data import cube_mentions, pivot_comments

U = get_user_model()
SERVICE_TOKEN = 'test-service-token-not-a-real-one'


@override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN)
class NewCommentRoutesRefuseEveryNonHumanTests(TestCase):
    """Four routes x three non-human credentials. None may write."""

    def setUp(self):
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()

        self.mc = U.objects.create_user(username='mc-gate', email='mc-gate@x.test',
                                        password='pass12345!')
        self.mc.is_superuser = True
        self.mc.is_staff = True
        self.mc.must_change_password = False
        if hasattr(self.mc, 'role'):
            self.mc.role = 'standard'
        self.mc.save()

        self.auditor = U.objects.create_user(username='aud-gate', email='aud-gate@x.test',
                                             password='pass12345!')
        self.auditor.must_change_password = False
        if hasattr(self.auditor, 'role'):
            self.auditor.role = 'auditor'
        self.auditor.save()

        self.addin = U.objects.create_user(username='excel-addin', email='addin@x.test')
        if hasattr(self.addin, 'role'):
            self.addin.role = 'service_readonly'
        self.addin.must_change_password = False
        self.addin.save()
        self.addin_token = Token.objects.create(user=self.addin)

        with connection.cursor() as c:
            c.execute("INSERT INTO app.cube_people (handle, display_name, email, active) "
                      "VALUES ('bk-gate','Book Keeper','bk-gate@x.test',true) "
                      "ON CONFLICT (handle) DO UPDATE SET active = true")
            c.execute("INSERT INTO app.cube_comments "
                      "(cell_key, subject_type, subject_key, tenant_id, measure, row_dims, "
                      " row_path, col_dims, col_path, filters, comment, author, author_key, status, tags) "
                      "VALUES ('k-gate','cube_cell','k-gate','t','amount','{}','{}','{}','', "
                      "'{}'::jsonb,'a note mentioning @bk-gate','MC','MC','open','{}') RETURNING id")
            self.cid = c.fetchone()[0]
        mail.outbox = []

    def tearDown(self):
        with connection.cursor() as c:
            c.execute("DELETE FROM app.cube_comments WHERE cell_key = 'k-gate'")
            c.execute("DELETE FROM app.cube_people WHERE handle = 'bk-gate'")

    # --- credentials, each a real header --------------------------------
    def jwt(self, u):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + str(RefreshToken.for_user(u).access_token)}

    def addin_cred(self):
        return {'HTTP_AUTHORIZATION': 'Token ' + self.addin_token.key}

    def service_cred(self):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + SERVICE_TOKEN}

    def routes(self):
        base = '/xero/data/journals/pivot/comments/%s/' % self.cid
        return [
            ('assign',  base + 'assign/',  {'assignee': 'bk-gate'}),
            ('text',    base + 'text/',    {'comment': 'rewritten by a machine'}),
            ('context', base + 'context/', {}),
            ('notify',  base + 'notify/',  {}),
        ]

    def _post(self, url, body, creds):
        import json
        return self.client.post(url, data=json.dumps(body),
                                content_type='application/json', **creds)

    # --- the table --------------------------------------------------------
    def test_auditor_may_not_write_to_any_new_route(self):
        for name, url, body in self.routes():
            with self.subTest(route=name):
                self.assertEqual(self._post(url, body, self.jwt(self.auditor)).status_code, 403)

    def test_the_addin_credential_may_not_write_to_any_new_route(self):
        for name, url, body in self.routes():
            with self.subTest(route=name):
                self.assertEqual(self._post(url, body, self.addin_cred()).status_code, 403)

    def test_the_shared_service_token_may_not_write_to_any_new_route(self):
        for name, url, body in self.routes():
            with self.subTest(route=name):
                r = self._post(url, body, self.service_cred())
                self.assertIn(r.status_code, (401, 403),
                              '%s accepted the shared MCP token' % name)

    def test_nothing_above_sent_mail(self):
        for _n, url, body in self.routes():
            for creds in (self.jwt(self.auditor), self.addin_cred(), self.service_cred()):
                self._post(url, body, creds)
        self.assertEqual(mail.outbox, [], 'a non-human credential sent mail')

    def test_nothing_above_changed_the_row(self):
        before = self._row()
        for _n, url, body in self.routes():
            for creds in (self.jwt(self.auditor), self.addin_cred(), self.service_cred()):
                self._post(url, body, creds)
        self.assertEqual(self._row(), before)

    def _row(self):
        with connection.cursor() as c:
            c.execute("SELECT comment, author_key, coalesce(assignee_role,''), "
                      "coalesce(decision,'') FROM app.cube_comments WHERE id = %s", [self.cid])
            return c.fetchone()

    # --- positive controls: the refusals above are the ROUTE, not the header
    def test_the_auditor_credential_itself_works(self):
        self.assertEqual(self.client.get('/audit/cube-comments/',
                                         **self.jwt(self.auditor)).status_code, 200)

    def test_the_addin_credential_itself_works(self):
        self.assertEqual(self.client.get('/xero/data/journals/filters/',
                                         **self.addin_cred()).status_code, 200)

    def test_a_named_human_CAN_do_all_of_it(self):
        for name, url, body in self.routes():
            with self.subTest(route=name):
                r = self._post(url, body, self.jwt(self.mc))
                self.assertLess(r.status_code, 400,
                                '%s refused MC: %s' % (name, r.content[:160]))
