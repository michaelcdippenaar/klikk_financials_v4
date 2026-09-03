"""
Regression suite for the forced-password-change flow.

An account created with a temporary password (``manage.py create_auditor``)
carries ``must_change_password``. AuditorGateMiddleware then holds it on the
auth endpoints plus ``POST /api/auth/change-password/`` and nothing else, so a
credential that was emailed or read down a phone line cannot stay in use.

Pins:

1. A flagged account gets 403 ``code: password_change_required`` on every
   other path — including the reads its ROLE would otherwise allow.
2. Changing the password requires the current one, and enforces Django's
   configured validators; a failed attempt leaves the flag set.
3. A successful change clears the flag, opens the gate, and rotates the
   credential (new password works, old one does not).
4. The flag is role-independent: a standard user is held the same way.
5. ``create_auditor`` sets the flag on create and leaves it alone on
   ``--convert``.

Run:
    python manage.py test apps.user.test_password_change -v 2
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

CHANGE_URL = '/api/auth/change-password/'
GOOD_PASSWORD = 'Kb7!zqrt-Landau'


def make_user(username, role, password='pass12345!', must_change=False):
    return User.objects.create_user(
        username=username,
        email=f'{username}@example.com',
        password=password,
        role=role,
        must_change_password=must_change,
    )


class PasswordChangeGateTests(TestCase):
    def setUp(self):
        self.flagged_auditor = make_user('flagaud', User.Role.AUDITOR, must_change=True)
        self.flagged_standard = make_user('flagstd', User.Role.STANDARD, must_change=True)
        self.clean_standard = make_user('cleanstd', User.Role.STANDARD)

    def jwt_client(self, username, password='pass12345!'):
        anon = APIClient()
        res = anon.post('/api/auth/login/', {'username': username, 'password': password}, format='json')
        assert res.status_code == 200, res.content
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['tokens']['access']}")
        return client, res.data

    # ── 1. The gate ─────────────────────────────────────────────────────────

    def test_flagged_auditor_blocked_from_reads_they_would_otherwise_have(self):
        client, _ = self.jwt_client('flagaud')
        res = client.get('/audit/findings/')
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(res.json()['code'], 'password_change_required')

    def test_flagged_standard_user_blocked_everywhere_too(self):
        """The flag is about the CREDENTIAL, not the role."""
        client, _ = self.jwt_client('flagstd')
        for path in ('/audit/findings/', '/xero/data/invoices/', '/api/pricelist/items/'):
            with self.subTest(path=path):
                res = client.get(path)
                self.assertEqual(res.status_code, 403, f'{path} -> {res.status_code}')
                self.assertEqual(res.json()['code'], 'password_change_required')

    def test_flagged_account_may_still_login_refresh_and_change(self):
        client, payload = self.jwt_client('flagaud')
        self.assertTrue(payload['user']['must_change_password'])
        res = client.post('/api/auth/refresh/', {'refresh': payload['tokens']['refresh']}, format='json')
        self.assertNotIn(res.status_code, (401, 403), res.content)
        res = client.post(CHANGE_URL, {}, format='json')
        self.assertEqual(res.status_code, 400, res.content)  # reached the VIEW, not the gate

    def test_unflagged_user_is_untouched(self):
        client, payload = self.jwt_client('cleanstd')
        self.assertFalse(payload['user']['must_change_password'])
        self.assertNotIn(client.get('/xero/data/invoices/').status_code, (401, 403))

    def test_anonymous_is_untouched(self):
        self.assertEqual(APIClient().get('/audit/findings/').status_code, 401)

    # ── 2. Rejections leave the flag set ────────────────────────────────────

    def test_wrong_current_password_is_400_and_flag_stays(self):
        client, _ = self.jwt_client('flagaud')
        res = client.post(
            CHANGE_URL,
            {'current_password': 'not-the-password', 'new_password': GOOD_PASSWORD},
            format='json',
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.flagged_auditor.refresh_from_db()
        self.assertTrue(self.flagged_auditor.must_change_password)
        self.assertTrue(self.flagged_auditor.check_password('pass12345!'))

    def test_weak_new_password_returns_validator_messages_and_flag_stays(self):
        client, _ = self.jwt_client('flagaud')
        res = client.post(
            CHANGE_URL,
            {'current_password': 'pass12345!', 'new_password': '123'},
            format='json',
        )
        self.assertEqual(res.status_code, 400, res.content)
        body = res.json()
        self.assertIn('detail', body)
        self.assertTrue(body['errors'], 'validator messages must reach the user')
        self.assertTrue(all(isinstance(m, str) for m in body['errors']))
        self.flagged_auditor.refresh_from_db()
        self.assertTrue(self.flagged_auditor.must_change_password)
        self.assertTrue(self.flagged_auditor.check_password('pass12345!'))

    def test_missing_fields_are_400(self):
        client, _ = self.jwt_client('flagaud')
        for body in ({}, {'current_password': 'pass12345!'}, {'new_password': GOOD_PASSWORD},
                     {'current_password': '', 'new_password': ''}):
            with self.subTest(body=body):
                self.assertEqual(client.post(CHANGE_URL, body, format='json').status_code, 400)
        self.flagged_auditor.refresh_from_db()
        self.assertTrue(self.flagged_auditor.must_change_password)

    def test_anonymous_change_password_is_401(self):
        res = APIClient().post(
            CHANGE_URL, {'current_password': 'x', 'new_password': GOOD_PASSWORD}, format='json')
        self.assertEqual(res.status_code, 401, res.content)

    # ── 3. The happy path ───────────────────────────────────────────────────

    def test_successful_change_clears_the_flag_and_opens_the_gate(self):
        client, _ = self.jwt_client('flagaud')
        res = client.post(
            CHANGE_URL,
            {'current_password': 'pass12345!', 'new_password': GOOD_PASSWORD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'changed': True})

        self.flagged_auditor.refresh_from_db()
        self.assertFalse(self.flagged_auditor.must_change_password)
        self.assertTrue(self.flagged_auditor.check_password(GOOD_PASSWORD))
        self.assertFalse(self.flagged_auditor.check_password('pass12345!'))

        # The SAME token now passes the gate — no re-login needed.
        self.assertNotIn(client.get('/audit/findings/').status_code, (401, 403))
        # …but the auditor role still applies.
        self.assertEqual(client.get('/xero/data/invoices/').status_code, 403)

    def test_old_password_stops_working_and_the_new_one_logs_in(self):
        client, _ = self.jwt_client('flagaud')
        client.post(
            CHANGE_URL,
            {'current_password': 'pass12345!', 'new_password': GOOD_PASSWORD},
            format='json',
        )
        anon = APIClient()
        self.assertEqual(
            anon.post('/api/auth/login/',
                      {'username': 'flagaud', 'password': 'pass12345!'}, format='json').status_code,
            401,
        )
        fresh = anon.post('/api/auth/login/',
                          {'username': 'flagaud', 'password': GOOD_PASSWORD}, format='json')
        self.assertEqual(fresh.status_code, 200, fresh.content)
        self.assertFalse(fresh.data['user']['must_change_password'])

    def test_unflagged_user_may_change_voluntarily(self):
        client, _ = self.jwt_client('cleanstd')
        res = client.post(
            CHANGE_URL,
            {'current_password': 'pass12345!', 'new_password': GOOD_PASSWORD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.clean_standard.refresh_from_db()
        self.assertFalse(self.clean_standard.must_change_password)
        self.assertTrue(self.clean_standard.check_password(GOOD_PASSWORD))

    def test_change_password_only_accepts_post(self):
        client, _ = self.jwt_client('cleanstd')
        for method in ('get', 'patch', 'delete', 'put'):
            with self.subTest(method=method):
                self.assertEqual(getattr(client, method)(CHANGE_URL).status_code, 405)


class MustChangePasswordDefaultsTests(TestCase):
    def test_default_is_false(self):
        user = User.objects.create_user(username='plainer', password='x12345678!')
        self.assertFalse(user.must_change_password)

    def test_create_auditor_flags_the_new_account(self):
        call_command('create_auditor', 'newaud', '--email', 'newaud@firm.co.za', stdout=StringIO())
        user = User.objects.get(username='newaud')
        self.assertEqual(user.role, User.Role.AUDITOR)
        self.assertTrue(user.must_change_password)

    def test_convert_leaves_the_flag_alone(self):
        """A converted account already has a password its owner chose."""
        User.objects.create_user(username='existing', password='x12345678!')
        call_command('create_auditor', 'existing', '--convert', stdout=StringIO())
        user = User.objects.get(username='existing')
        self.assertEqual(user.role, User.Role.AUDITOR)
        self.assertFalse(user.must_change_password)


class PasswordChangeMiddlewareUnitTests(TestCase):
    """Path/method logic only — no live views, no DB user needed."""

    def _allowed(self, method, path):
        from django.test import RequestFactory

        from apps.user.middleware import AuditorGateMiddleware
        request = getattr(RequestFactory(), method.lower())(path)
        return AuditorGateMiddleware(lambda r: None)._password_change_allowed(request)

    def _auditor_allowed(self, method, path):
        from django.test import RequestFactory

        from apps.user.middleware import AuditorGateMiddleware
        request = getattr(RequestFactory(), method.lower())(path)
        return AuditorGateMiddleware(lambda r: None)._allowed(request)

    def test_only_auth_and_change_password_survive_the_flag(self):
        self.assertTrue(self._allowed('POST', CHANGE_URL))
        self.assertTrue(self._allowed('POST', '/api/auth/login/'))
        self.assertTrue(self._allowed('POST', '/api/auth/refresh/'))
        self.assertTrue(self._allowed('OPTIONS', '/audit/findings/'))
        for method, path in (
            ('GET', '/audit/findings/'),
            ('GET', '/audit/receipts/'),
            ('POST', '/audit/findings/1/comments/'),
            ('GET', '/xero/data/invoices/'),
            ('GET', '/admin/'),
            ('POST', '/api/auth/register/'),
            ('POST', '/api/auth/change-password'),     # no trailing slash
            ('POST', '/api/auth/change-password/x/'),  # deeper
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(self._allowed(method, path))

    def test_auditors_may_reach_change_password_after_the_flag_clears(self):
        self.assertTrue(self._auditor_allowed('POST', CHANGE_URL))
