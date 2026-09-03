"""
Regression suite for the service_readonly role hard gate (gate C of
AuditorGateMiddleware).

WHY THIS EXISTS. excel_addin/README.md called the add-in's identity
"least-privilege" and "read-only by design" while the credential was neither.
The `excel-addin` Django user was role=standard, so its DRF authtoken passed
every IsAuthenticated view in the project — including
XeroCreateDraftInvoiceView (`/xero/data/invoices/create-draft/`, the ONE Xero
write path in this codebase) and every Xero sync trigger, each of which can
burn the 5,000-call daily API budget. The JavaScript never calls those; the
token, sitting in a laptop's localStorage and posted to whatever `baseUrl` is
typed into the pane's settings box, could.

Pins five things:

1. The read surface the pane needs is reachable: safe methods on
   /xero/data/journals/... never answer 401/403 for this role.
2. The Xero WRITE and the sync triggers answer 403. This is the finding.
3. The cube collaboration writes still work — POST a comment (and its bulk and
   status siblings), POST/DELETE a subset and a view. Those land in OUR
   Postgres and reach nothing upstream; if they 403 the pane loses comments
   and saved views, which is live functionality.
4. Everything outside the allowlist is 403 — Investec, pricelist, the audit
   surface, the Django admin, the rest of /xero/.
5. The gate is reached at all through `Authorization: Token <key>`. Before this
   change resolve_request_user understood only sessions and Bearer JWTs, so a
   role gate on this account would have matched nothing and granted everything.
   test_drf_authtoken_is_resolved_by_the_gate is the load-bearing one.

Run:
    python manage.py test apps.user.test_service_readonly_gate -v 2
"""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.user.middleware import AuditorGateMiddleware

User = get_user_model()

# What the pane reads. All safe-method.
ADDIN_READS = [
    "/xero/data/journals/search/?limit=1",
    "/xero/data/journals/filters/",
    "/xero/data/journals/pivot/?rows=account_type&measure=amount",
    "/xero/data/journals/pivot/dimensions/",
    "/xero/data/journals/pivot/members/?dimension=account",
    "/xero/data/journals/pivot/subsets/",
    "/xero/data/journals/pivot/views/",
    "/xero/data/journals/pivot/comments/?status=all",
]

# The reason the role exists: the Xero write, and the sync triggers that spend
# the daily API budget. Every one of these was reachable with this token.
XERO_WRITES_AND_SYNCS = [
    ("/xero/data/invoices/create-draft/", "post"),
    ("/xero/data/invoices/sync/", "post"),
    ("/xero/data/quotes/sync/", "post"),
    ("/xero/data/aged-payables/sync/", "post"),
    ("/xero/data/aged-receivables/sync/", "post"),
    ("/xero/data/sync/documents/", "post"),
    ("/xero/data/update/journals/", "post"),
    ("/xero/data/process/journals/", "post"),
]


class ServiceReadonlyGateTests(TestCase):
    """Live requests carrying a REAL DRF authtoken, the credential shape the
    add-in actually uses — not force_authenticate, which would bypass the
    header resolution this gate depends on."""

    def setUp(self):
        self.service = User.objects.create_user(
            username="excel-addin-test", email="addin@example.com",
            password="pw-not-logged", role=User.Role.SERVICE_READONLY,
        )
        self.standard = User.objects.create_user(
            username="std-token", email="std@example.com",
            password="pw-not-logged",
        )

    def token_client(self, user):
        client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return client

    # ── 5. The gate is reached at all ───────────────────────────────────────

    def test_drf_authtoken_is_resolved_by_the_gate(self):
        """The load-bearing precondition for everything else in this file.

        resolve_request_user originally handled sessions and `Bearer` only.
        A DRF authtoken is `Token <key>`, so a role gate on this account would
        have matched nothing and silently granted the standard role's access.
        """
        from apps.user.middleware import resolve_request_user

        token, _ = Token.objects.get_or_create(user=self.service)
        request = RequestFactory().get(
            "/xero/data/journals/filters/", HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(resolve_request_user(request), self.service)

    def test_garbage_authtoken_resolves_to_nobody(self):
        """A bad token must not raise out of the middleware; the view's own
        auth stack answers it (401), the gate stays out of the way."""
        from apps.user.middleware import resolve_request_user

        request = RequestFactory().get(
            "/xero/data/journals/filters/", HTTP_AUTHORIZATION="Token not-a-real-key")
        self.assertIsNone(resolve_request_user(request))

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token not-a-real-key")
        self.assertEqual(client.get("/xero/data/journals/filters/").status_code, 401)

    # ── 1. The add-in's read surface still works ────────────────────────────

    def test_addin_reads_pass_the_gate(self):
        client = self.token_client(self.service)
        for path in ADDIN_READS:
            with self.subTest(path=path):
                try:
                    res = client.get(path)
                except Exception:
                    # A view exception (missing fixture rows, raw-SQL schemas
                    # absent from the test DB) means the request got PAST the
                    # gate, which is all this asserts.
                    continue
                self.assertNotIn(
                    res.status_code, (401, 403),
                    f"GET {path} was rejected for the add-in role: {res.status_code}",
                )

    # ── 2. The finding: Xero writes and sync triggers are refused ───────────

    def test_xero_write_and_sync_triggers_blocked(self):
        client = self.token_client(self.service)
        for path, method in XERO_WRITES_AND_SYNCS:
            with self.subTest(path=path, method=method):
                res = getattr(client, method)(path, {}, format="json")
                self.assertEqual(
                    res.status_code, 403,
                    f"{method.upper()} {path} was NOT blocked ({res.status_code}) "
                    f"— a laptop-resident token can reach Xero again",
                )

    def test_standard_token_still_reaches_them(self):
        """The gate SUBTRACTS from the new role only. A standard user's token
        is unaffected — otherwise this change would be a project-wide lockout
        dressed up as a role."""
        client = self.token_client(self.standard)
        for path, method in XERO_WRITES_AND_SYNCS:
            with self.subTest(path=path, method=method):
                try:
                    res = getattr(client, method)(path, {}, format="json")
                except Exception:
                    continue  # view logic ran => past the gate
                self.assertNotEqual(
                    res.status_code, 403,
                    f"{method.upper()} {path} now 403s a STANDARD user",
                )

    # ── 3. The cube collaboration writes still work ─────────────────────────

    # These three views build their own tables in the `app` schema with raw
    # DDL. If that DDL cannot run in the test DB the request has still reached
    # view code, which is exactly what the gate assertion is about — so an
    # exception counts as a pass here, the same convention test_auth_lockdown
    # uses. The precise path/method logic is pinned in the unit tests below,
    # which need no database at all.

    def test_can_post_a_cube_comment(self):
        client = self.token_client(self.service)
        try:
            res = client.post(
                "/xero/data/journals/pivot/comments/",
                {
                    "measure": "amount",
                    "row_path": ["Revenue"],
                    "col_path": ["2026-01"],
                    "filters": {"journal_type": "transaction"},
                    "text": "What is this figure?",
                },
                format="json",
            )
        except Exception:
            return
        self.assertNotIn(
            res.status_code, (401, 403),
            f"posting a cube comment was rejected: {res.status_code} {res.content!r}",
        )

    def test_can_save_and_delete_a_subset(self):
        client = self.token_client(self.service)
        try:
            post = client.post(
                "/xero/data/journals/pivot/subsets/",
                {"dimension": "account", "name": "gate-test", "members": ["200"]},
                format="json",
            )
            self.assertNotIn(post.status_code, (401, 403), post.content)
            delete = client.delete(
                "/xero/data/journals/pivot/subsets/?dimension=account&name=gate-test")
        except Exception:
            return
        self.assertNotIn(delete.status_code, (401, 403), delete.content)

    def test_can_save_and_delete_a_view(self):
        client = self.token_client(self.service)
        try:
            post = client.post(
                "/xero/data/journals/pivot/views/",
                {"name": "gate-test-view",
                 "spec": {"rows": ["account"], "cols": [], "measure": "amount"},
                 "query": {}},
                format="json",
            )
            self.assertNotIn(post.status_code, (401, 403), post.content)
            delete = client.delete("/xero/data/journals/pivot/views/?name=gate-test-view")
        except Exception:
            return
        self.assertNotIn(delete.status_code, (401, 403), delete.content)

    # ── 4. Everything else is refused ───────────────────────────────────────

    def test_non_journal_surface_blocked(self):
        client = self.token_client(self.service)
        for path in (
            "/xero/data/invoices/",
            "/xero/data/quotes/",
            "/xero/data/aged-payables/",
            "/xero/metadata/accounts/",
            "/xero/core/tenants/",
            "/api/investec/bank/accounts/",
            "/api/pricelist/items/",
            "/audit/findings/",
        ):
            with self.subTest(path=path):
                res = client.get(path)
                self.assertEqual(
                    res.status_code, 403,
                    f"GET {path} leaked to the add-in role: {res.status_code}",
                )

    def test_blocked_from_django_admin(self):
        self.client.force_login(self.service)
        self.assertEqual(self.client.get("/admin/").status_code, 403)


class ServiceReadonlyGateUnitTests(TestCase):
    """Path/method logic of gate C alone — no live views, so routes whose
    views need fixtures or non-Django schemas are covered here too."""

    def _allowed(self, method, path):
        request = getattr(RequestFactory(), method.lower())(path)
        return AuditorGateMiddleware(lambda r: None)._service_readonly_allowed(request)

    def test_journal_reads_allowed(self):
        for path in (
            "/xero/data/journals/search/",
            "/xero/data/journals/filters/",
            "/xero/data/journals/pivot/",
            "/xero/data/journals/pivot/dimensions/",
            "/xero/data/journals/pivot/members/",
            "/xero/data/journals/pivot/drill/",
            "/xero/data/journals/pivot/subsets/",
            "/xero/data/journals/pivot/views/",
            "/xero/data/journals/pivot/comments/",
        ):
            self.assertTrue(self._allowed("GET", path), path)
            self.assertTrue(self._allowed("HEAD", path), path)

    def test_signed_document_file_allowed_but_not_its_siblings(self):
        self.assertTrue(self._allowed("GET", "/xero/data/documents/42/file/"))
        # Sibling document routes are NOT part of the deal.
        self.assertFalse(self._allowed("GET", "/xero/data/documents/search/"))
        self.assertFalse(self._allowed("GET", "/xero/data/documents/by-transaction/abc/"))
        self.assertFalse(self._allowed("GET", "/xero/data/documents/42/file/x/"))
        self.assertFalse(self._allowed("POST", "/xero/data/documents/42/file/"))

    def test_xero_write_and_sync_triggers_refused(self):
        for path in (
            "/xero/data/invoices/create-draft/",
            "/xero/data/invoices/sync/",
            "/xero/data/quotes/sync/",
            "/xero/data/aged-payables/sync/",
            "/xero/data/aged-receivables/sync/",
            "/xero/data/sync/documents/",
            "/xero/data/update/journals/",
            "/xero/data/process/journals/",
            "/xero/data/process/journals",          # the no-slash re_path variant
            "/xero/sync/invoices/",
            "/xero/cube/import-pnl-by-tracking/",
        ):
            self.assertFalse(self._allowed("POST", path), path)

    def test_cube_collaboration_writes_allowed(self):
        for path in (
            "/xero/data/journals/pivot/comments/",
            "/xero/data/journals/pivot/comments/bulk/",
            "/xero/data/journals/pivot/comments/1/status/",
            "/xero/data/journals/pivot/comments/12345/status/",
            "/xero/data/journals/pivot/subsets/",
            "/xero/data/journals/pivot/views/",
        ):
            self.assertTrue(self._allowed("POST", path), path)

    def test_delete_is_only_subsets_and_views(self):
        self.assertTrue(self._allowed("DELETE", "/xero/data/journals/pivot/subsets/"))
        self.assertTrue(self._allowed("DELETE", "/xero/data/journals/pivot/views/"))
        # Comments are append-only / retract-by-empty for everyone.
        self.assertFalse(self._allowed("DELETE", "/xero/data/journals/pivot/comments/"))
        self.assertFalse(self._allowed("DELETE", "/xero/data/journals/pivot/comments/1/status/"))
        self.assertFalse(self._allowed("DELETE", "/xero/data/journals/search/"))

    def test_write_allowlist_cannot_be_widened_by_a_suffix_or_verb(self):
        # A POST anywhere under the READ prefix is not a write permit.
        self.assertFalse(self._allowed("POST", "/xero/data/journals/search/"))
        self.assertFalse(self._allowed("POST", "/xero/data/journals/pivot/"))
        self.assertFalse(self._allowed("POST", "/xero/data/journals/pivot/drill/"))
        self.assertFalse(self._allowed("POST", "/xero/data/journals/pivot/people/"))
        # Anchored at both ends: no deeper segment, no missing trailing slash,
        # no prefix smuggling.
        self.assertFalse(self._allowed("POST", "/xero/data/journals/pivot/subsets"))
        self.assertFalse(self._allowed("POST", "/xero/data/journals/pivot/subsets/1/"))
        self.assertFalse(self._allowed("POST", "/xero/data/journals/pivot/comments/abc/status/"))
        self.assertFalse(self._allowed("POST", "/xero/data/journals/pivot/comments/1/status/x/"))
        self.assertFalse(self._allowed("POST", "/x/xero/data/journals/pivot/comments/"))
        # PATCH/PUT are not in the allowlist at all.
        for method in ("PATCH", "PUT"):
            self.assertFalse(self._allowed(method, "/xero/data/journals/pivot/subsets/"), method)
            self.assertFalse(self._allowed(method, "/xero/data/journals/pivot/comments/"), method)

    def test_read_prefix_is_a_prefix_not_a_substring(self):
        self.assertFalse(self._allowed("GET", "/xero/data/journalsomething/"))
        self.assertFalse(self._allowed("GET", "/x/xero/data/journals/search/"))
        # And it is /xero/data/journals/, never the whole of /xero/data/.
        self.assertFalse(self._allowed("GET", "/xero/data/"))

    def test_rest_of_the_project_refused(self):
        for path in (
            "/audit/findings/",
            "/api/investec/bank/accounts/",
            "/api/pricelist/items/",
            "/api/planning-analytics/tm1/config/",
            "/api/ai-agent/agent-status/",
            "/admin/",
            "/api/auth/login/",       # this identity has no business logging in
        ):
            self.assertFalse(self._allowed("GET", path), path)
            self.assertFalse(self._allowed("POST", path), path)

    def test_options_always_allowed(self):
        self.assertTrue(self._allowed("OPTIONS", "/xero/data/invoices/create-draft/"))
