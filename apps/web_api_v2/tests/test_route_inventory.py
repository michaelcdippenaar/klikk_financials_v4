from django.conf import settings
from django.test import SimpleTestCase
from django.urls import resolve

from apps.web_api_v2 import auth_urls, entity_urls
from apps.web_api_v2.auth_views import LoginView, LogoutView, RefreshView, VerifyView
from apps.web_api_v2.share_mapping_views import ShareMappingView
from apps.web_api_v2.ingest_views import (
    ProcessRunDetailView,
    ProcessRunListCreateView,
    ProcessStatusView,
)
from apps.web_api_v2.schema import schema


class WebApiV2RouteInventoryTests(SimpleTestCase):
    def test_exact_browser_auth_inventory(self):
        self.assertEqual(len(auth_urls.urlpatterns), 4)
        expected = {
            '/api/v2/auth/login/': LoginView,
            '/api/v2/auth/refresh/': RefreshView,
            '/api/v2/auth/verify/': VerifyView,
            '/api/v2/auth/logout/': LogoutView,
        }
        for path, view_class in expected.items():
            self.assertIs(resolve(path).func.view_class, view_class)

    def test_exact_entity_ingest_inventory(self):
        # This count is a deliberate gate: a new entity-scoped endpoint has to
        # be added here on purpose, so one cannot appear unnoticed. The fourth
        # is the share-mapping command — the second V2 write, carrying its own
        # MANAGE_SHARE_MAPPINGS capability rather than riding on the one that
        # runs syncs.
        self.assertEqual(len(entity_urls.urlpatterns), 4)
        expected = {
            '/api/v2/entities/entity-a/ingest/process-runs/': ProcessRunListCreateView,
            '/api/v2/entities/entity-a/ingest/process-status/': ProcessStatusView,
            '/api/v2/entities/entity-a/ingest/process-runs/00000000-0000-0000-0000-000000000001/':
                ProcessRunDetailView,
            '/api/v2/entities/entity-a/investec/share-mappings/': ShareMappingView,
        }
        for path, view_class in expected.items():
            self.assertIs(resolve(path).func.view_class, view_class)

    def test_graphql_remains_read_only(self):
        rendered = schema.as_str()
        self.assertIn('ingestOverview', rendered)
        self.assertNotIn('type Mutation', rendered)

    def test_v2_rate_and_request_size_limits_are_configured(self):
        rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
        self.assertEqual(rates['v2_auth_login'], '10/min')
        self.assertEqual(rates['v2_auth_refresh'], '30/min')
        self.assertEqual(rates['v2_auth_verify'], '60/min')
        self.assertEqual(rates['v2_auth_logout'], '30/min')
        self.assertEqual(rates['v2_ingest_reads'], '120/min')
        self.assertEqual(rates['v2_ingest_commands'], '10/hour')
        self.assertEqual(settings.WEB_API_V2_AUTH_MAX_REQUEST_BYTES, 16 * 1024)
        self.assertEqual(settings.WEB_API_V2_INGEST_MAX_REQUEST_BYTES, 16 * 1024)
        self.assertEqual(settings.WEB_API_V2_INGEST_RUN_LEASE_SECONDS, 1800)
