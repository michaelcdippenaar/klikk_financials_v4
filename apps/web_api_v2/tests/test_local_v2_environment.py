import json
import os
from unittest import skipUnless

from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import resolve

from apps.web_api_v2.management.commands.seed_local_v2 import (
    SYNTHETIC_ENTITY_ID,
    SYNTHETIC_USERNAME,
)
from apps.web_api_v2.models import (
    IngestProcessAuditEvent,
    IngestProcessRun,
    IngestSourceJobDefinition,
    UserEntityCapability,
    UserEntityMembership,
)
from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken


LOCAL_SETTINGS = getattr(settings, "LOCAL_V2_SAFE_MODE", False)
VIEWER_QUERY = """
query ViewerContext {
  viewerContext {
    user { username }
    entities { id name capabilities }
    preferences { defaultEntityId defaultFinancialYear }
  }
}
"""
PIPELINE_QUERY = """
query LocalPipeline($context: FinancialContextInput!) {
  xeroPipelineSummary(context: $context) {
    context { entityId financialYear }
    stages { key state nextValidAction }
  }
  overviewIngestSources(context: $context) {
    sources { key availabilityCode actions { kind permitted } }
  }
}
"""


@skipUnless(LOCAL_SETTINGS, "requires the dedicated local V2 settings module")
class LocalV2EnvironmentTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.environ["LOCAL_V2_SYNTHETIC_PASSWORD"] = "local-v2-test-password-only"

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        call_command("seed_local_v2", verbosity=0)

    def _login(self):
        response = self.client.post(
            "/api/v2/auth/login/",
            data=json.dumps({
                "username": SYNTHETIC_USERNAME,
                "password": os.environ["LOCAL_V2_SYNTHETIC_PASSWORD"],
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["tokens"]["access"]

    def test_local_settings_disable_external_surfaces(self):
        self.assertEqual(
            settings.ROOT_URLCONF,
            "klikk_business_intelligence.local_v2_urls",
        )
        self.assertFalse(settings.XERO_SCHEDULER_ENABLED)
        self.assertEqual(settings.KLIKK_API_TOKEN, "")
        self.assertFalse(settings.AI_AGENT_PAW_ENABLED)
        self.assertFalse(settings.AI_AGENT_WEB_SEARCH_ENABLED)
        self.assertFalse(settings.AI_AGENT_GOOGLE_DRIVE_ENABLED)
        self.assertEqual(settings.INVESTEC_BASE_URL, "http://127.0.0.1:1")
        self.assertNotIn("apps.ai_agent", settings.INSTALLED_APPS)
        self.assertNotIn("apps.investec", settings.INSTALLED_APPS)
        self.assertNotIn("apps.planning_analytics", settings.INSTALLED_APPS)
        self.assertNotIn("apps.deployment", settings.INSTALLED_APPS)

    def test_urlconf_exposes_only_health_auth_and_graphql(self):
        self.assertEqual(resolve("/health/local-v2/").url_name, "local-v2-health")
        self.assertEqual(resolve("/api/v2/graphql/").url_name, "graphql")
        self.assertEqual(resolve("/api/v2/auth/login/").url_name, "login")
        forbidden = (
            f"/api/v2/entities/{SYNTHETIC_ENTITY_ID}/ingest/process-runs/",
            "/api/auth/login/",
            "/xero/callback/",
            "/api/investec/accounts/",
            "/api/planning-analytics/",
            "/api/ai-agent/",
            "/deployment/webhook/",
        )
        for path in forbidden:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 404)

    def test_fixture_is_view_only_and_contains_no_source_credentials_or_runs(self):
        membership = UserEntityMembership.objects.get(
            user__username=SYNTHETIC_USERNAME,
            entity_id=SYNTHETIC_ENTITY_ID,
        )
        self.assertTrue(membership.active)
        self.assertEqual(membership.role, UserEntityMembership.Role.VIEWER)
        self.assertFalse(UserEntityCapability.objects.exists())
        self.assertFalse(IngestProcessRun.objects.exists())
        self.assertFalse(IngestProcessAuditEvent.objects.exists())
        self.assertFalse(XeroClientCredentials.objects.exists())
        self.assertFalse(XeroTenantToken.objects.exists())
        definitions = IngestSourceJobDefinition.objects.filter(entity_id=SYNTHETIC_ENTITY_ID)
        self.assertEqual(definitions.count(), 8)
        self.assertFalse(
            definitions.exclude(
                configuration_state=(
                    IngestSourceJobDefinition.ConfigurationState.NOT_CONFIGURED
                ),
                supported_operations=[],
            ).exists()
        )

    def test_real_auth_and_viewer_context_are_synthetic_and_view_only(self):
        token = self._login()
        response = self.client.post(
            "/api/v2/graphql/",
            data=json.dumps({"query": VIEWER_QUERY, "operationName": "ViewerContext"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        context = response.json()["data"]["viewerContext"]
        self.assertEqual(context["user"]["username"], SYNTHETIC_USERNAME)
        self.assertEqual(len(context["entities"]), 1)
        self.assertEqual(context["entities"][0]["id"], SYNTHETIC_ENTITY_ID)
        self.assertEqual(
            context["entities"][0]["capabilities"],
            ["VIEW_FINANCIALS"],
        )
        self.assertEqual(context["preferences"]["defaultEntityId"], SYNTHETIC_ENTITY_ID)

    def test_operational_commands_are_absent_even_with_valid_jwt(self):
        token = self._login()
        response = self.client.post(
            f"/api/v2/entities/{SYNTHETIC_ENTITY_ID}/ingest/process-runs/",
            data=json.dumps({
                "processKey": "metadata",
                "idempotencyKey": "must-not-run",
                "periods": ["2025-07"],
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(IngestProcessRun.objects.exists())

    def test_read_models_are_not_configured_and_actions_are_disabled(self):
        token = self._login()
        response = self.client.post(
            "/api/v2/graphql/",
            data=json.dumps({
                "query": PIPELINE_QUERY,
                "operationName": "LocalPipeline",
                "variables": {
                    "context": {
                        "entityId": SYNTHETIC_ENTITY_ID,
                        "financialYear": 2026,
                        "periodSelection": {"mode": "ALL"},
                    }
                },
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("errors", body)
        self.assertEqual(len(body["data"]["overviewIngestSources"]["sources"]), 8)
        xero = body["data"]["overviewIngestSources"]["sources"][0]
        self.assertEqual(xero["key"], "XERO")
        self.assertEqual(xero["availabilityCode"], "NOT_CONFIGURED")
        self.assertFalse(any(action["permitted"] for action in xero["actions"]))
        self.assertTrue(all(
            stage["state"] == "NOT_CONFIGURED"
            for stage in body["data"]["xeroPipelineSummary"]["stages"]
        ))

    def test_foreign_entity_is_denied_without_leaking_existence(self):
        token = self._login()
        response = self.client.post(
            "/api/v2/graphql/",
            data=json.dumps({
                "query": PIPELINE_QUERY,
                "operationName": "LocalPipeline",
                "variables": {
                    "context": {
                        "entityId": "local-v2-foreign-entity",
                        "financialYear": 2026,
                        "periodSelection": {"mode": "ALL"},
                    }
                },
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["errors"][0]["extensions"]["code"],
            "FORBIDDEN_ENTITY",
        )

    def test_graphql_remains_query_only(self):
        token = self._login()
        response = self.client.post(
            "/api/v2/graphql/",
            data=json.dumps({"query": "mutation LocalWrite { unsupported }"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("errors", response.json())
