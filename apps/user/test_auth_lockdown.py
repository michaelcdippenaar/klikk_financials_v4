"""
Regression suite for the 2026-08-20 auth lockdown (SECURITY-NOTE.md).

Pins three things:

1. The DRF-wide default is IsAuthenticated: a representative sweep of every
   formerly-anonymous endpoint family returns 401 to an anonymous caller and
   is NOT auth-rejected for an authenticated one. The sweep asserts the GATE,
   not endpoint health — an authed response may be 200/400/404/500 depending
   on fixtures, but never 401/403.
2. The public allowlist stays exactly as designed: login, refresh, token/*,
   nginx-check, api-token-auth, the Xero OAuth callback, and the HMAC-signed
   slip viewer. Nothing else.
3. POST /api/auth/register/ no longer hands out JWTs anonymously: it is
   IsAdminUser (anon 401, plain user 403, staff 201-with-tokens).

Anonymous 401s are produced by the permission layer, which DRF runs BEFORE
throttling, so this suite does not consume the anon throttle bucket.

Run:
    python manage.py test apps.user.test_auth_lockdown -v 2
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.audit.slip_view import slip_signature

User = get_user_model()

# Every endpoint family SECURITY-NOTE.md §2 listed as anonymously readable,
# plus the §3 anonymous mutators that survived routing. One representative URL
# per view. (path, method) — bodies are empty; a 400 from view logic still
# proves the gate passed, a 401 proves it held.
GATED = [
    # xero_data — the 7 reads flagged by the add-in session
    ("/xero/data/documents/by-transaction/some-txn-id/", "get"),
    ("/xero/data/aged-payables/", "get"),
    ("/xero/data/aged-receivables/", "get"),
    ("/xero/data/quotes/", "get"),
    ("/xero/data/quotes/QU-0001/", "get"),
    ("/xero/data/invoices/", "get"),
    ("/xero/data/invoices/INV-0001/", "get"),
    # xero_data — journal reads (gated 2026-08-19, re-pinned here).
    # The whole cube surface, not just the two the original sweep listed: this
    # IS the Excel add-in's read surface, and excel_addin/README.md names four
    # endpoints that "must stay IsAuthenticated" while only two of them were
    # pinned anywhere. Every one of these returns journal detail or aggregates
    # over all 271,764 Klikk journal lines.
    ("/xero/data/journals/search/?limit=1", "get"),
    ("/xero/data/journals/filters/", "get"),
    ("/xero/data/journals/pivot/?rows=account_type&measure=amount", "get"),
    ("/xero/data/journals/pivot/dimensions/", "get"),
    ("/xero/data/journals/pivot/members/?dimension=account", "get"),
    ("/xero/data/journals/pivot/drill/", "get"),
    ("/xero/data/journals/pivot/subsets/", "get"),
    ("/xero/data/journals/pivot/views/", "get"),
    # xero metadata / core / auth / sync / validation
    ("/xero/metadata/contacts/", "get"),
    ("/xero/metadata/accounts/", "get"),
    ("/xero/metadata/tracking/", "get"),
    ("/xero/core/tenants/", "get"),
    ("/xero/auth/status/", "get"),
    ("/xero/auth/initiate/", "get"),
    ("/xero/auth/credentials/", "post"),
    ("/xero/sync/api-call-stats/", "get"),
    ("/xero/validation/import-profit-loss/", "get"),  # the GET that burned Xero quota
    ("/xero/validation/export-trail-balance/", "get"),
    ("/xero/validation/balance-sheet/", "get"),
    # Investec
    ("/api/investec/bank/accounts/", "get"),
    ("/api/investec/bank/transactions/", "get"),
    ("/api/investec/bank/transactions/export/", "get"),
    ("/api/investec/bank/beneficiaries/", "get"),
    ("/api/investec/bank/sync/", "post"),
    # Personal expenses
    ("/api/personal-expenses/report/", "get"),
    ("/api/personal-expenses/transactions/", "get"),
    # Financial investments
    ("/api/financial-investments/symbols/", "get"),
    ("/api/financial-investments/symbols/AAPL/refresh/", "post"),
    # Audit registry (the raw-SQL surface)
    ("/audit/checks/", "get"),
    ("/audit/checks/", "post"),
    ("/audit/run/", "post"),
    # Pricelist reads (HasServiceToken must no longer pass anonymous reads)
    ("/api/pricelist/items/", "get"),
    ("/api/pricelist/export/", "get"),
    ("/api/pricelist/quote/", "post"),
    # Planning analytics
    ("/api/planning-analytics/tm1/config/", "get"),
    ("/api/planning-analytics/tm1/config/", "post"),
    ("/api/planning-analytics/tm1/processes/", "get"),
    # AI agent
    ("/api/ai-agent/agent-status/", "get"),
    ("/api/ai-agent/ws/broadcast/", "post"),
]

AUTH_REJECTED = (401, 403)

# Endpoints whose VIEW LOGIC answers 403 for app-level reasons (not a
# permission class): asserting "authed != 403" would be wrong for these.
# /xero/auth/initiate/ returns 403 "No active Xero credentials found" when the
# (test) DB holds no XeroClientCredentials row.
# Endpoints whose 403 comes from VIEW LOGIC ("no active Xero credentials
# configured"), not from the permission gate. The authenticated sweep must not
# read these as a gate that is stricter than IsAuthenticated.
# /xero/core/tenants/ joined this set on 2026-08-20: it used to answer 500 here
# (unguarded .get() -> DoesNotExist), which the sweep tolerated. It now answers
# the same honest 403 as /xero/auth/initiate/ when the table has no credentials.
VIEW_LOGIC_403 = {"/xero/auth/initiate/", "/xero/core/tenants/"}


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="lockdown-user", email="lockdown@example.com",
            password="pw-not-logged",
        )
        cls.staff = User.objects.create_user(
            username="lockdown-staff", email="staff@example.com",
            password="pw-not-logged", is_staff=True,
        )

    def setUp(self):
        self.client = APIClient()


class AnonymousSweepTests(_Base):
    """Every formerly-public endpoint must 401 an anonymous caller."""

    def test_anonymous_gets_401(self):
        for path, method in GATED:
            with self.subTest(endpoint=f"{method.upper()} {path}"):
                resp = getattr(self.client, method)(path)
                self.assertEqual(
                    resp.status_code, 401,
                    f"{method.upper()} {path}: anonymous caller got "
                    f"{resp.status_code}, expected 401 — this endpoint is "
                    f"exposed to the internet again",
                )


class AuthenticatedSweepTests(_Base):
    """The same endpoints must accept an authenticated caller.

    Asserts the gate only: authed responses may be 200/400/404/405/500
    depending on fixtures in the test DB, but never 401/403 (403 would mean a
    gate stricter than IsAuthenticated crept in and the console would break).
    """

    def test_authenticated_not_auth_rejected(self):
        self.client.force_authenticate(self.user)
        for path, method in GATED:
            with self.subTest(endpoint=f"{method.upper()} {path}"):
                try:
                    resp = getattr(self.client, method)(path)
                except Exception:
                    # The test client re-raises view exceptions. An exception
                    # from view logic (missing fixture rows, absent raw-SQL
                    # schemas in the test DB) means the request got PAST the
                    # permission layer, which is all this sweep asserts.
                    continue
                rejected = (401,) if path in VIEW_LOGIC_403 else AUTH_REJECTED
                self.assertNotIn(
                    resp.status_code, rejected,
                    f"{method.upper()} {path}: authenticated caller got "
                    f"{resp.status_code} — the console/MCP would break here",
                )


class RegistrationLockTests(_Base):
    """POST /api/auth/register/ is IsAdminUser since 2026-08-20."""

    URL = "/api/auth/register/"
    BODY = {
        "username": "new-user", "email": "new@example.com",
        "password": "S3cure!pass-word", "password_confirm": "S3cure!pass-word",
    }

    def test_anonymous_cannot_register(self):
        resp = self.client.post(self.URL, self.BODY)
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(User.objects.filter(username="new-user").exists())

    def test_plain_user_cannot_register(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post(self.URL, self.BODY)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(User.objects.filter(username="new-user").exists())

    def test_staff_can_create_user(self):
        self.client.force_authenticate(self.staff)
        resp = self.client.post(self.URL, self.BODY)
        self.assertEqual(resp.status_code, 201)
        created = User.objects.get(username="new-user")
        self.assertFalse(created.is_staff)
        self.assertFalse(created.is_superuser)


class PublicAllowlistTests(_Base):
    """The credential bootstrap paths and the signed slip viewer stay public."""

    def test_login_reachable_anonymously(self):
        # Empty body -> 400 from view logic. A 401 here would mean login
        # itself got gated and nobody could ever authenticate.
        resp = self.client.post("/api/auth/login/", {})
        self.assertEqual(resp.status_code, 400)

    def test_refresh_reachable_anonymously(self):
        resp = self.client.post("/api/auth/refresh/", {})
        self.assertEqual(resp.status_code, 400)

    def test_token_obtain_reachable_anonymously(self):
        resp = self.client.post("/api/auth/token/", {})
        self.assertEqual(resp.status_code, 400)

    def test_api_token_auth_reachable_anonymously(self):
        # The Excel add-in's token bootstrap (DRF ObtainAuthToken).
        resp = self.client.post("/api-token-auth/", {})
        self.assertEqual(resp.status_code, 400)

    def test_nginx_check_returns_401_without_cookie_not_before_view(self):
        # The view itself answers 401 (no cookie); the point is that it is
        # routed and reachable, not rejected by a permission class with a
        # DRF error body.
        resp = self.client.get("/api/auth/nginx-check/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.content, b"")

    def test_xero_callback_reachable_anonymously(self):
        # No ?code= -> redirect to the frontend error page, NOT a 401.
        resp = self.client.get("/xero/callback/")
        self.assertEqual(resp.status_code, 302)

    def test_slip_viewer_rejects_bad_signature_not_auth(self):
        sha = "0" * 64
        resp = self.client.get(f"/audit/slip/{sha}/?s=bad-signature")
        self.assertEqual(resp.status_code, 403)  # HMAC failure, not a login wall

    def test_slip_viewer_accepts_valid_signature_anonymously(self):
        # Valid HMAC for a sha that is not in the DB: passes the signature
        # gate, proving no auth layer sits in front. In the test DB the
        # whatsapp.klikk_slips schema does not exist, so reaching the query
        # (ProgrammingError) is the same proof as the 404.
        from django.db import ProgrammingError

        sha = "0" * 64
        try:
            resp = self.client.get(f"/audit/slip/{sha}/?s={slip_signature(sha)}")
        except ProgrammingError:
            return
        self.assertEqual(resp.status_code, 404)


class AuthenticatedWithoutXeroCredentialsTests(TestCase):
    """The defect the lockdown INTRODUCED, and why an anonymous-only test misses it.

    The sweep above asserts the permission GATE and explicitly tolerates a 500
    from an authenticated caller ("may be 200/400/404/500"). That blind spot is
    exactly where this bug lived.

    Pre-lockdown these views were AllowAny, so real traffic arrived anonymous and
    took the safe ``.filter(active=True).first()`` branch. The lockdown made every
    caller authenticated, which pushed real users onto the sibling branch --
    ``XeroClientCredentials.objects.get(user=request.user, active=True)`` -- and
    that raises DoesNotExist for any logged-in user who does not personally own a
    credentials row. Unhandled exception, so DRF answered **500**. Observed live
    on GET /xero/core/tenants/ from the console.

    The load-bearing case is a user who IS authenticated and has NO credentials
    row. A test that only checks the anonymous 401 passes today and would never
    catch a regression here.
    """

    def setUp(self):
        self.client = APIClient()
        # Deliberately owns no XeroClientCredentials row.
        self.stranger = User.objects.create_user(
            username="no-xero-credentials", password="pw-not-logged")

    def test_tenants_does_not_500_for_authenticated_user_without_credentials(self):
        self.client.force_authenticate(self.stranger)
        resp = self.client.get("/xero/core/tenants/")
        self.assertNotEqual(
            resp.status_code, 500,
            "GET /xero/core/tenants/ raised for an authenticated user with no "
            "credentials row -- the .get()/DoesNotExist regression is back")
        # With no credentials in the table at all, the honest answer is 403.
        self.assertEqual(resp.status_code, 403)

    def test_tenants_is_still_401_anonymously(self):
        resp = self.client.get("/xero/core/tenants/")
        self.assertEqual(resp.status_code, 401)

    def test_resolver_never_raises_and_prefers_the_users_own_row(self):
        """Unit-level pin on the shared helper the three views now use."""
        from django.test import RequestFactory

        from apps.xero.xero_auth.credentials import (
            resolve_active_credentials, resolve_active_credentials_user)
        from apps.xero.xero_auth.models import XeroClientCredentials

        request = RequestFactory().get("/")
        request.user = self.stranger

        # Nothing in the table: None, not an exception.
        self.assertIsNone(resolve_active_credentials(request))
        self.assertIsNone(resolve_active_credentials_user(request))

        owner = User.objects.create_user(username="xero-owner", password="pw-not-logged")
        owned = XeroClientCredentials.objects.create(
            user=owner, client_id="cid-owner", client_secret="secret",
            scope=[], active=True)

        # A user with no row of their own falls back to the active row rather
        # than raising. Deliberate single-operator behaviour -- see the module
        # docstring in apps/xero/xero_auth/credentials.py.
        self.assertEqual(resolve_active_credentials(request), owned)

        # A user WITH their own row gets their own, never the fallback.
        mine = XeroClientCredentials.objects.create(
            user=self.stranger, client_id="cid-mine", client_secret="secret",
            scope=[], active=True)
        self.assertEqual(resolve_active_credentials(request), mine)

    def test_service_token_caller_does_not_500_on_credential_resolution(self):
        """The MCP server authenticates as a non-persisted ServiceAccount.

        ServiceAccount has is_authenticated=True but pk=None and is not a
        Django model, so passing it to .filter(user=...) raises -- which would
        turn every machine call into a 500. Caught by
        xero_sync.test_trigger_auth while fixing the tenants defect; pinned
        here so the resolver keeps handling it.
        """
        from django.test import RequestFactory

        from apps.xero.xero_auth.credentials import resolve_active_credentials
        from klikk_business_intelligence.permissions import ServiceAccount

        request = RequestFactory().get("/")
        request.user = ServiceAccount()
        self.assertIsNone(resolve_active_credentials(request))  # must not raise

    def test_no_view_reintroduces_the_unguarded_get(self):
        """LIVING GUARD over the whole family, not just the one view that broke.

        The same shape existed in xero_core, xero_metadata and xero_sync. This
        fails if anyone reintroduces ``.get(user=request.user`` in a view module,
        which is the precise construct that turns a 4xx into a 500.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "apps" / "xero"
        offenders = []
        for path in root.rglob("views*.py"):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # prose ABOUT the construct is not the construct
                if ".get(user=request.user" in stripped:
                    offenders.append(f"{path}:{lineno}: {stripped}")
        self.assertEqual(
            offenders, [],
            "Unguarded .get(user=request.user ...) is back -- it raises "
            "DoesNotExist and DRF turns that into a 500. Use "
            "apps/xero/xero_auth/credentials.resolve_active_credentials():\n  "
            + "\n  ".join(offenders))
