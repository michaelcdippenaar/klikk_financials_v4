"""
Regression suite for the auditor role hard gate (AuditorGateMiddleware).

Pins four things:

1. An auditor (role=auditor) can READ the audit surface: GET /audit/...
   never answers 401/403 for them (endpoint health aside — 200/400/404 all
   prove the gate passed).
2. An auditor gets 403 on every write anywhere — including writes on the
   audit surface itself (findings bulk, receipt review/bulk actions,
   attachment uploads, every PATCH/DELETE).
2b. The ONE exception: POST of a comment on a finding or a receipt is
   allowed, and the stored comment is attributed to the auditor's own
   username. Nothing else about the role changes.
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

from apps.audit.models import AuditFinding, AuditFindingComment

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

# NOTE: /audit/findings/<pk>/comments/ is deliberately absent — POSTing a
# comment is the one write auditors may make (see AUDITOR_WRITE_RE and
# test_auditor_can_post_finding_comment below).
WRITES_EVERYWHERE = [
    ("/audit/findings/bulk/", "post"),
    ("/audit/findings/1/", "patch"),
    ("/audit/findings/1/attachments/", "post"),
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
        self.finding = AuditFinding.objects.create(
            fy=2026, ref="FY26-001", title="Gate fixture finding",
            severity="MEDIUM", status="OPEN", category="OTHER",
            source="internal-audit run 1", created_by="seed",
        )

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

    # ── 2b. The ONE write an auditor may make: a comment ────────────────────

    def test_auditor_can_post_finding_comment(self):
        client, _ = self.jwt_client("aud1")
        res = client.post(
            f"/audit/findings/{self.finding.pk}/comments/",
            {"text": "Please supply the supporting invoice."},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        comment = AuditFindingComment.objects.get(finding=self.finding)
        self.assertEqual(comment.text, "Please supply the supporting invoice.")
        # Attribution is the auditor's own username — in production that is
        # their email address, which is exactly the wanted audit trail.
        self.assertEqual(comment.author, self.auditor.get_username())
        self.assertEqual(res.data["author"], self.auditor.get_username())

    def test_auditor_comment_on_missing_finding_is_404_not_403(self):
        """The gate lets the request through; the VIEW decides it does not exist."""
        client, _ = self.jwt_client("aud1")
        res = client.post("/audit/findings/999999/comments/", {"text": "x"}, format="json")
        self.assertEqual(res.status_code, 404, res.content)

    def test_auditor_comment_write_does_not_widen_the_role(self):
        """Neighbouring writes on the SAME finding stay blocked."""
        client, _ = self.jwt_client("aud1")
        pk = self.finding.pk
        for path, method in (
            (f"/audit/findings/{pk}/", "patch"),
            (f"/audit/findings/{pk}/attachments/", "post"),
            (f"/audit/findings/{pk}/links/", "post"),
            (f"/audit/findings/{pk}/comments/", "delete"),
            ("/audit/findings/bulk/", "post"),
        ):
            with self.subTest(path=path, method=method):
                res = getattr(client, method)(path, {}, format="json")
                self.assertEqual(
                    res.status_code, 403,
                    f"{method.upper()} {path} was not blocked: {res.status_code}",
                )

    def test_standard_user_can_still_comment(self):
        client, _ = self.jwt_client("std1")
        res = client.post(
            f"/audit/findings/{self.finding.pk}/comments/",
            {"text": "noted"}, format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["author"], self.standard.get_username())

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

    def test_comment_posts_allowed(self):
        self.assertTrue(self._allowed("POST", "/audit/findings/1/comments/"))
        self.assertTrue(self._allowed("POST", "/audit/findings/12345/comments/"))
        # Receipts: covered here rather than live because the receipts views hit
        # the raw whatsapp.klikk_slips table, absent from the test DB.
        self.assertTrue(self._allowed("POST", "/audit/receipts/abc123/comments/"))
        self.assertTrue(self._allowed("POST", "/audit/receipts/" + "a" * 64 + "/comments/"))

    def test_comment_exception_is_narrow(self):
        # Wrong verb on the allowed path.
        for method in ("PATCH", "PUT", "DELETE"):
            self.assertFalse(self._allowed(method, "/audit/findings/1/comments/"), method)
            self.assertFalse(self._allowed(method, "/audit/receipts/abc123/comments/"), method)
        # Not-quite-the-path variants.
        self.assertFalse(self._allowed("POST", "/audit/findings/1/comments"))        # no trailing /
        self.assertFalse(self._allowed("POST", "/audit/findings/1/comments/1/"))     # deeper
        self.assertFalse(self._allowed("POST", "/audit/findings/comments/"))         # no pk
        self.assertFalse(self._allowed("POST", "/audit/findings/abc/comments/"))     # non-numeric pk
        self.assertFalse(self._allowed("POST", "/audit/receipts/a.b/comments/"))     # dot in key
        self.assertFalse(self._allowed("POST", "/audit/receipts/a/b/comments/"))     # slash in key
        self.assertFalse(self._allowed("POST", "/x/audit/findings/1/comments/"))     # not anchored
        # Neighbouring writes stay shut.
        self.assertFalse(self._allowed("POST", "/audit/findings/bulk/"))
        self.assertFalse(self._allowed("POST", "/audit/findings/1/attachments/"))
        self.assertFalse(self._allowed("POST", "/audit/findings/1/links/"))
        self.assertFalse(self._allowed("PATCH", "/audit/findings/1/"))
        self.assertFalse(self._allowed("POST", "/audit/receipts/abc123/review/"))
        self.assertFalse(self._allowed("PATCH", "/audit/receipts/abc123/review/"))
        self.assertFalse(self._allowed("POST", "/audit/receipts/bulk/"))
        self.assertFalse(self._allowed("DELETE", "/audit/findings/attachments/1/"))

    def test_options_always_allowed(self):
        self.assertTrue(self._allowed("OPTIONS", "/xero/data/invoices/"))
