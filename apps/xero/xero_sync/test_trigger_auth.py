"""
Adversarial auth tests for the Xero *trigger* endpoints.

These endpoints each start real Xero API traffic when they succeed, against a
tenant capped at 1,000 calls/day. They were gated (AllowAny -> IsAuthenticated)
on 2026-08-20 (SECURITY-NOTE.md §3). This suite tries to PROVE the gate does
not hold: that an anonymous / spoofed / malformed caller can still reach the
underlying Xero service.

ZERO network: every underlying service is patched, so even if a gate were
broken the test could not emit a live Xero call. The whole point of the
`assert_not_called()` assertions is that a 401 returned *after* work has begun
is still a hole -- we watch the service, not just the status code.

Lives as a module file (test_trigger_auth.py), not under a tests/ package,
to match the working layout of the sibling test_api_call_stats.py and to stay
discoverable.

Run:
    python manage.py test apps.xero.xero_sync.test_trigger_auth -v 2
"""
import base64
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import Resolver404, get_resolver, resolve, reverse
from django.contrib.auth import get_user_model
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.xero.xero_core.models import XeroTenant

User = get_user_model()

SERVICE_TOKEN = "test-service-token-abc123-do-not-log"

# (label, reverse_name, patch_target, http_method)
# patch_target is patched WHERE USED (in the view module), and is the exact
# service that would burn Xero quota. If an anonymous request ever reaches it,
# the gate is a hole.
GATED_XERO_TRIGGERS = [
    ("update-models", "xero_sync:xero-update-models",
     "apps.xero.xero_sync.views.update_xero_models", "post"),
    ("update-metadata", "xero_metadata:update_metadata",
     "apps.xero.xero_metadata.views.update_metadata", "post"),
    ("reconcile-post", "xero_validation:reconcile_reports",
     "apps.xero.xero_validation.views.reconciliation_views.reconcile_reports_for_financial_year", "post"),
    ("reconcile-get", "xero_validation:reconcile_reports",
     "apps.xero.xero_validation.views.reconciliation_views.reconcile_reports_for_financial_year", "get"),
    ("update-data", "xero_data:update_data",
     "apps.xero.xero_data.views.update_financial_data", "post"),
    ("sync-documents", "xero_data:sync_documents",
     "apps.xero.xero_data.views.sync_documents_for_tenant", "post"),
    ("sync-aged-payables", "xero_data:sync_aged_payables",
     "apps.xero.xero_data.views.sync_aged_payables", "post"),
    ("sync-aged-receivables", "xero_data:sync_aged_receivables",
     "apps.xero.xero_data.views.sync_aged_receivables", "post"),
    ("sync-quotes", "xero_data:sync_quotes",
     "apps.xero.xero_data.views.sync_xero_quotes", "post"),
    ("sync-invoices", "xero_data:sync_invoices",
     "apps.xero.xero_data.views.sync_xero_invoices", "post"),
]

REJECTED = (401, 403)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = XeroTenant.objects.create(
            tenant_id="trigger-auth-tenant", tenant_name="TriggerAuth Co"
        )
        cls.user = User.objects.create_user(
            username="trigger-auth-user", email="ta@example.com", password="pw-not-logged"
        )

    def setUp(self):
        self.client = APIClient()

    def _call(self, name, method, **kwargs):
        url = reverse(name)
        return getattr(self.client, method)(url, {"tenant_id": self.tenant.tenant_id,
                                                  "financial_year": 2024}, **kwargs)


class AnonymousGatedTriggerTests(_Base):
    """Anonymous callers must be rejected BEFORE any Xero service runs."""

    def test_anonymous_rejected_and_service_not_called(self):
        for label, name, target, method in GATED_XERO_TRIGGERS:
            with self.subTest(endpoint=label):
                with patch(target) as mock_service:
                    resp = self._call(name, method)
                    self.assertIn(
                        resp.status_code, REJECTED,
                        f"{label}: anonymous {method.upper()} got {resp.status_code}, "
                        f"expected 401/403",
                    )
                    mock_service.assert_not_called()


class ReconcileVerbCoverageTests(_Base):
    """The console uses the GET on ReconcileReportsView, and GET delegates to
    the same _run() as POST -- so a gate on only one verb is a hole. Prove BOTH
    verbs are gated and neither reaches the reconcile service."""

    def test_reconcile_get_gated(self):
        with patch("apps.xero.xero_validation.views.reconciliation_views."
                   "reconcile_reports_for_financial_year") as m:
            resp = self._call("xero_validation:reconcile_reports", "get")
            self.assertIn(resp.status_code, REJECTED)
            m.assert_not_called()

    def test_reconcile_post_gated(self):
        with patch("apps.xero.xero_validation.views.reconciliation_views."
                   "reconcile_reports_for_financial_year") as m:
            resp = self._call("xero_validation:reconcile_reports", "post")
            self.assertIn(resp.status_code, REJECTED)
            m.assert_not_called()


class AuthBypassHeaderTests(_Base):
    """Malformed / spoofed credentials must not slip past the gate on a
    representative gated endpoint (xero-update-models)."""

    NAME = "xero_sync:xero-update-models"
    TARGET = "apps.xero.xero_sync.views.update_xero_models"

    def _post(self, **extra):
        url = reverse(self.NAME)
        return self.client.post(url, {"tenant_id": self.tenant.tenant_id}, **extra)

    def test_garbage_authorization_headers_rejected(self):
        unsigned_jwt = ("eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
                        "eyJ1c2VyX2lkIjoxLCJ0b2tlbl90eXBlIjoiYWNjZXNzIn0.")
        basic = "Basic " + base64.b64encode(b"admin:admin").decode()
        headers = [
            ("bearer-empty", "Bearer"),
            ("bearer-garbage", "Bearer garbage"),
            ("unsigned-jwt", f"Bearer {unsigned_jwt}"),
            ("token-scheme", "Token abc"),
            ("basic-auth", basic),
            ("lowercase-bearer-garbage", "bearer garbage"),
        ]
        for label, value in headers:
            with self.subTest(header=label):
                with patch(self.TARGET) as m:
                    resp = self._post(HTTP_AUTHORIZATION=value)
                    self.assertIn(resp.status_code, REJECTED,
                                  f"{label}: got {resp.status_code}")
                    m.assert_not_called()

    def test_ip_spoofing_headers_do_not_bypass(self):
        for label, extra in [
            ("xff", {"HTTP_X_FORWARDED_FOR": "127.0.0.1"}),
            ("xreal", {"HTTP_X_REAL_IP": "127.0.0.1"}),
        ]:
            with self.subTest(spoof=label):
                with patch(self.TARGET) as m:
                    resp = self._post(**extra)
                    self.assertIn(resp.status_code, REJECTED)
                    m.assert_not_called()

    def test_trailing_slash_variant_does_not_reach_service(self):
        # /xero/sync/update  (no trailing slash) must not sneak past.
        with patch(self.TARGET) as m:
            resp = self.client.post("/xero/sync/update",
                                    {"tenant_id": self.tenant.tenant_id})
            self.assertNotEqual(resp.status_code, 200)
            m.assert_not_called()


class _CredentialedBase(_Base):
    """Adds an active XeroClientCredentials row.

    Since 2026-08-20 the sync/metadata triggers resolve credentials BEFORE
    calling the service layer, and answer a clean 403 when the system has no
    Xero connection at all (previously they passed the caller straight through
    and blew up deeper, in XeroApiClient, as a 500). These tests exist to prove
    that AUTH reaches the service layer, so they need the system to be
    connected -- otherwise they assert the 403 precondition instead of the
    auth plumbing they were written for."""

    def setUp(self):
        super().setUp()
        from apps.xero.xero_auth.models import XeroClientCredentials
        XeroClientCredentials.objects.get_or_create(
            user=self.user, active=True,
            defaults={"client_id": "cid-test", "client_secret": "secret", "scope": []},
        )


@override_settings(KLIKK_API_TOKEN=SERVICE_TOKEN)
class ServiceTokenTests(_CredentialedBase):
    """ServiceTokenAuthentication runs before JWT and the MCP server relies on
    it. The correct token must AUTHENTICATE (not 401); a wrong token must not."""

    NAME = "xero_sync:xero-update-models"
    TARGET = "apps.xero.xero_sync.views.update_xero_models"

    def test_correct_service_token_authenticates(self):
        with patch(self.TARGET) as m:
            m.return_value = {"success": True, "message": "ok", "stats": {}}
            resp = self.client.post(
                reverse(self.NAME), {"tenant_id": self.tenant.tenant_id},
                HTTP_AUTHORIZATION=f"Bearer {SERVICE_TOKEN}",
            )
            self.assertNotIn(resp.status_code, REJECTED,
                             f"service token was rejected ({resp.status_code}) -- "
                             f"this breaks the MCP server")
            self.assertEqual(resp.status_code, 200)
            m.assert_called_once()

    def test_wrong_service_token_rejected(self):
        with patch(self.TARGET) as m:
            resp = self.client.post(
                reverse(self.NAME), {"tenant_id": self.tenant.tenant_id},
                HTTP_AUTHORIZATION="Bearer wrong-service-token",
            )
            self.assertIn(resp.status_code, REJECTED)
            m.assert_not_called()


class JwtHappyPathTests(_CredentialedBase):
    """A real simplejwt access token must reach the endpoint -- this is what
    proves the console's Run buttons still work. Downstream mocked."""

    def test_jwt_reaches_update_models(self):
        token = str(AccessToken.for_user(self.user))
        with patch("apps.xero.xero_sync.views.update_xero_models") as m:
            m.return_value = {"success": True, "message": "ok", "stats": {}}
            resp = self.client.post(
                reverse("xero_sync:xero-update-models"),
                {"tenant_id": self.tenant.tenant_id},
                HTTP_AUTHORIZATION=f"Bearer {token}",
            )
            self.assertEqual(resp.status_code, 200)
            m.assert_called_once()

    def test_jwt_reaches_reconcile_get(self):
        token = str(AccessToken.for_user(self.user))
        with patch("apps.xero.xero_validation.views.reconciliation_views."
                   "reconcile_reports_for_financial_year") as m:
            m.return_value = {"api_calls": 0}
            resp = self.client.get(
                reverse("xero_validation:reconcile_reports"),
                {"tenant_id": self.tenant.tenant_id, "financial_year": 2024},
                HTTP_AUTHORIZATION=f"Bearer {token}",
            )
            self.assertEqual(resp.status_code, 200)
            m.assert_called_once()


class FormerNegativeControlsNowGatedTests(_Base):
    """CONTRACT INVERTED 2026-08-20. When the triggers were gated (19 Aug)
    these read endpoints were deliberately left anonymous so the quota widget
    / process page kept working. The full lockdown (SECURITY-NOTE.md,
    DEFAULT_PERMISSION_CLASSES=IsAuthenticated) closed them the next day; the
    console now sends a JWT everywhere. Anonymous must be 401, authenticated
    must get past the permission layer."""

    GATED_READS = ["xero_sync:xero-api-call-stats", "xero_sync:xero-process-status",
                   "xero_data:aged_payables_list", "xero_data:aged_receivables_list",
                   "xero_data:quotes_list", "xero_data:invoices_list"]

    def test_reads_now_401_anonymously(self):
        for name in self.GATED_READS:
            with self.subTest(endpoint=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(
                    resp.status_code, 401,
                    f"{name} answered {resp.status_code} to an anonymous "
                    f"caller: the lockdown regressed",
                )

    def test_reads_pass_the_gate_for_an_authenticated_user(self):
        # Some require a tenant_id query param and answer 400 without it --
        # a 400 still proves the request passed the permission layer.
        self.client.force_authenticate(self.user)
        for name in self.GATED_READS:
            with self.subTest(endpoint=name):
                resp = self.client.get(reverse(name))
                self.assertIn(resp.status_code, (200, 400))


class WebhookRemovedTests(TestCase):
    """POST /deployment/webhook/github/ shell-executed for anonymous callers.
    It must be gone at the resolver AND the router AND the urlconf."""

    def test_resolve_raises(self):
        with self.assertRaises(Resolver404):
            resolve("/deployment/webhook/github/")

    def test_anonymous_post_404(self):
        resp = APIClient().post("/deployment/webhook/github/", {})
        self.assertEqual(resp.status_code, 404)

    def test_deployment_urlpatterns_empty(self):
        from apps.deployment import urls as deployment_urls
        self.assertEqual(list(deployment_urls.urlpatterns), [])


# --- Anti-regression sweeps ------------------------------------------------
# These introspect the resolver only (no HTTP, no network).

import re

TRIGGER_RE = re.compile(r"\b(update|process|sync|import|refresh|run|execute)\b")

# Xero endpoints intentionally left anonymous (negative controls) -- reads only.
_ALLOWED_OPEN_XERO_ROUTES = {
    "xero/sync/api-call-stats/",
    "xero/sync/process-status/",
}

# DELIBERATE AllowAny EXEMPTIONS. Currently EMPTY, and that is the point.
#
# Anything listed here is a trigger-shaped Xero route that is knowingly left
# AllowAny -- which means an anonymous internet caller can burn the tenant's
# 1,000/day API budget. The list started with two such holes
# (xero_validation:import_profit_loss and xero_validation:validate_complete),
# both missed by the first gating pass and both caught by the living sweep
# below; the SECURITY-NOTE.md lockdown closed them, so the list emptied.
#
# It earns its keep while empty. The living sweep subtracts these routes, so a
# hole can only be tolerated by being written down here, in a diff someone
# reviews; and the tripwire asserts every entry is genuinely still AllowAny, so
# an entry whose hole gets fixed must be deleted rather than left to rot into a
# stale exemption. Empty list + tripwire = a new exemption has to be added on
# purpose, and cannot be added silently.
_KNOWN_UNGATED_HOLES = {
    # route: (reverse_name, view_class_name, why)
}


def _iter_routes(resolver, prefix=""):
    for p in resolver.url_patterns:
        route = str(p.pattern)
        if hasattr(p, "url_patterns"):
            yield from _iter_routes(p, prefix + route)
        else:
            cls = getattr(p.callback, "cls", None) or getattr(p.callback, "view_class", None)
            yield prefix + route, cls


class AntiRegressionSweepTests(TestCase):

    def test_no_new_ungated_xero_triggers(self):
        """LIVING GUARD: every trigger-shaped path under xero/ must NOT be
        AllowAny, EXCEPT the documented negative controls and any deliberate
        exemption written into _KNOWN_UNGATED_HOLES (currently none). A
        newly-added ungated Xero trigger fails here."""
        violations = []
        for route, cls in _iter_routes(get_resolver()):
            if not route.startswith("xero/"):
                continue
            if route in _ALLOWED_OPEN_XERO_ROUTES or route in _KNOWN_UNGATED_HOLES:
                continue
            if not TRIGGER_RE.search(route):
                continue
            if cls is None:
                continue
            if AllowAny in getattr(cls, "permission_classes", []):
                violations.append(f"{route} -> {cls.__name__} is AllowAny")
        self.assertEqual(
            violations, [],
            "NEW ungated Xero trigger endpoint(s) found (each burns the "
            "1,000/day tenant budget for any anonymous caller):\n  "
            + "\n  ".join(violations)
            + "\nGate it (IsAuthenticated), or if it is deliberately open add it "
              "to _ALLOWED_OPEN_XERO_ROUTES with justification.",
        )

    def test_gated_reconcile_stays_gated(self):
        """Positive assertion: the Xero report-pull endpoint that WAS gated
        (ReconcileReportsView) must stay IsAuthenticated / not AllowAny."""
        match = resolve(reverse("xero_validation:reconcile_reports"))
        cls = getattr(match.func, "cls", None) or getattr(match.func, "view_class", None)
        self.assertNotIn(AllowAny, getattr(cls, "permission_classes", []))

    def test_known_holes_are_still_open_tripwire(self):
        """TRIPWIRE. Asserts every entry in _KNOWN_UNGATED_HOLES is STILL
        AllowAny, so a hole that gets fixed must be deleted from the list
        rather than lingering as a stale exemption.

        The list is empty as of 2026-08-20, so this currently passes
        vacuously -- the property it protects only bites once someone adds an
        exemption. That is intended: the guard that keeps the list empty is
        test_no_new_ungated_xero_triggers, which no longer subtracts anything.
        """
        fixed = []
        for route, (name, view_name, _why) in _KNOWN_UNGATED_HOLES.items():
            match = resolve(reverse(name))
            cls = getattr(match.func, "cls", None) or getattr(match.func, "view_class", None)
            if AllowAny not in getattr(cls, "permission_classes", []):
                fixed.append(f"{name} ({view_name}) now appears GATED")
        self.assertEqual(
            fixed, [],
            "A known hole looks fixed -- remove it from _KNOWN_UNGATED_HOLES:\n  "
            + "\n  ".join(fixed),
        )
