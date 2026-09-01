"""
Regression suite for the auditor role hard gate (AuditorGateMiddleware).

Pins four things:

1. An auditor (role=auditor) can READ the audit surface: GET /audit/...
   never answers 401/403 for them (endpoint health aside — 200/400/404 all
   prove the gate passed).
2. An auditor gets 403 on every write anywhere — including writes on the
   audit surface itself (findings bulk, comments, receipt actions).
3. An auditor gets 403 on every non-audit read (Xero GL, Investec, pricelist,
   dashboards) — the middleware blocks BEFORE view logic, so this holds even
   for endpoints whose own permission is just IsAuthenticated.
4. Standard users and anonymous callers are completely unaffected: the gate
   only subtracts from auditor accounts. Login still returns the role so the
   frontend can shape the UI.

Run:
    python manage.py test apps.user.test_auditor_gate -v 2
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

# NOTE: /audit/receipts/ is deliberately absent — its view queries the raw
# whatsapp.klikk_slips table, which does not exist in the Django test DB and
# aborts the test transaction. Its gate behaviour (same /audit/ prefix) is
# pinned by the middleware unit tests below instead.
AUDIT_READS = [
    "/audit/findings/",
    "/audit/findings/summary/",
    "/audit/checks/",
]

NON_AUDIT_READS = [
    "/xero/data/invoices/",
    "/xero/data/journals/search/?limit=1",
    "/api/investec/bank/accounts/",
    "/api/pricelist/items/",
]

WRITES_EVERYWHERE = [
    ("/audit/findings/bulk/", "post"),
    ("/audit/findings/1/comments/", "post"),
    ("/audit/findings/1/", "patch"),
    ("/xero/sync/invoices/", "post"),
    ("/api/pricelist/items/", "post"),
]


def make_user(username, role, password="pass12345!"):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=password,
        role=role,
    )


class AuditorGateTests(TestCase):
    def setUp(self):
        self.auditor = make_user("aud1", User.Role.AUDITOR)
        self.standard = make_user("std1", User.Role.STANDARD)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def jwt_client(self, username):
        """A client holding a REAL JWT — exercises the middleware's own
        token resolution rather than DRF's force_authenticate shortcut."""
        anon = APIClient()
        res = anon.post(
            "/api/auth/login/",
            {"username": username, "password": "pass12345!"},
            format="json",
        )
        assert res.status_code == 200, res.content
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['tokens']['access']}")
        return client, res.data

    # ── 1. Auditor reads on /audit/ pass the gate ───────────────────────────

    def test_auditor_can_read_audit_surface(self):
        client, _ = self.jwt_client("aud1")
        for path in AUDIT_READS:
            with self.subTest(path=path):
                res = client.get(path)
                self.assertNotIn(
                    res.status_code, (401, 403),
                    f"{path} auth-rejected an auditor: {res.status_code}",
                )

    # ── 2. Auditor writes are blocked everywhere ────────────────────────────

    def test_auditor_writes_blocked(self):
        client, _ = self.jwt_client("aud1")
        for path, method in WRITES_EVERYWHERE:
            with self.subTest(path=path, method=method):
                res = getattr(client, method)(path, {}, format="json")
                self.assertEqual(
                    res.status_code, 403,
                    f"{method.upper()} {path} was not blocked: {res.status_code}",
                )

    # ── 3. Auditor non-audit reads are blocked ──────────────────────────────

    def test_auditor_non_audit_reads_blocked(self):
        client, _ = self.jwt_client("aud1")
        for path in NON_AUDIT_READS:
            with self.subTest(path=path):
                res = client.get(path)
                self.assertEqual(
                    res.status_code, 403,
                    f"GET {path} leaked to an auditor: {res.status_code}",
                )

    def test_auditor_blocked_from_django_admin(self):
        # Session path: log the auditor into a session, hit the admin.
        self.client.force_login(self.auditor)
        res = self.client.get("/admin/")
        self.assertEqual(res.status_code, 403)

    # ── 4. Everyone else unaffected ─────────────────────────────────────────

    def test_standard_user_unaffected(self):
        client, _ = self.jwt_client("std1")
        for path in AUDIT_READS + NON_AUDIT_READS:
            with self.subTest(path=path):
                res = client.get(path)
                self.assertNotIn(
                    res.status_code, (401, 403),
                    f"{path} auth-rejected a standard user: {res.status_code}",
                )

    def test_anonymous_unaffected_still_401(self):
        anon = APIClient()
        res = anon.get("/audit/findings/")
        self.assertEqual(res.status_code, 401)  # gate stays out of the way

    def test_login_returns_role_and_refresh_allowed(self):
        client, payload = self.jwt_client("aud1")
        self.assertEqual(payload["user"]["role"], "auditor")
        # Refresh with the Bearer header attached (the axios client does this).
        res = client.post(
            "/api/auth/refresh/",
            {"refresh": payload["tokens"]["refresh"]},
            format="json",
        )
        self.assertNotIn(res.status_code, (401, 403), res.content)

    def test_default_role_is_standard(self):
        user = User.objects.create_user(username="plain", password="x12345678!")
        self.assertEqual(user.role, User.Role.STANDARD)
        self.assertFalse(user.is_auditor)


class AuditorGateUnitTests(TestCase):
    """Path/method logic of the middleware itself — no live views involved,
    so surfaces whose views need non-Django tables (receipts) are covered."""

    def _allowed(self, method, path):
        from django.test import RequestFactory
        from apps.user.middleware import AuditorGateMiddleware
        request = getattr(RequestFactory(), method.lower())(path)
        return AuditorGateMiddleware(lambda r: None)._allowed(request)

    def test_receipts_reads_allowed(self):
        self.assertTrue(self._allowed("GET", "/audit/receipts/"))
        self.assertTrue(self._allowed("GET", "/audit/receipts/abc123/view/"))
        self.assertTrue(self._allowed("GET", "/audit/slip/abc123/"))
        self.assertTrue(self._allowed("GET", "/audit/findings/export/"))
        self.assertTrue(self._allowed("GET", "/audit/findings/attachments/1/file/"))

    def test_receipts_writes_blocked(self):
        self.assertFalse(self._allowed("POST", "/audit/receipts/"))
        self.assertFalse(self._allowed("POST", "/audit/receipts/bulk/"))
        self.assertFalse(self._allowed("DELETE", "/audit/findings/attachments/1/"))
        self.assertFalse(self._allowed("POST", "/audit/checks/SUP-01/run/"))
        self.assertFalse(self._allowed("POST", "/audit/run/"))

    def test_non_audit_blocked_and_auth_allowed(self):
        self.assertFalse(self._allowed("GET", "/xero/data/invoices/"))
        self.assertFalse(self._allowed("GET", "/api/investec/bank/accounts/"))
        self.assertFalse(self._allowed("GET", "/api/whatsapp/chats/"))
        self.assertFalse(self._allowed("GET", "/admin/"))
        self.assertFalse(self._allowed("GET", "/auditors-are-not-here/"))  # prefix, not substring
        self.assertTrue(self._allowed("POST", "/api/auth/login/"))
        self.assertTrue(self._allowed("POST", "/api/auth/refresh/"))
        self.assertTrue(self._allowed("POST", "/api/auth/token/refresh/"))
        self.assertFalse(self._allowed("POST", "/api/auth/register/"))

    def test_options_always_allowed(self):
        self.assertTrue(self._allowed("OPTIONS", "/xero/data/invoices/"))
