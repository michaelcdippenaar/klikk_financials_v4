"""End-to-end browser contract tests.

Every other suite in this app exercises one resolver or one view. Nothing
proved the whole path a browser actually takes: sign in, receive a token,
present it to GraphQL, and get a usable answer scoped to an entity the user
is really a member of.

That gap is why two defects survived a green build. The frontend hardcoded a
July-June fiscal year while the backend resolves it per entity, and a live
page shipped querying a field that was not in the deployed schema. Unit tests
cannot see either; only running the real documents over the real transport can.

The documents below are the exact operations Klikk Portal V2 sends. Their
source files are named against each one. If a document here drifts from the
frontend, this suite stops testing what the browser does — keep them in step.

The fixture entity deliberately starts its fiscal year in MARCH. A July
default would let the regression back in unnoticed.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.web_api_v2.models import UserEntityMembership
from apps.xero.xero_core.models import XeroTenant


# src/v2/api/viewerContext.js
VIEWER_CONTEXT = '''
query ViewerContext {
  viewerContext {
    user { id username displayName email }
    entitySelectionState
    entities { id name role active status capabilities }
    preferences { defaultEntityId defaultFinancialYear }
  }
}
'''

# src/v2/api/ingest.js
LIVE_OVERVIEW_SOURCES = '''
query LiveOverviewSources($context: FinancialContextInput!) {
  overviewIngestSources(context: $context) {
    context { entityId financialYear fiscalYearStartMonth selectionMode selectedPeriods }
    actionableAttentionCount
    sources {
      key label provider mode availabilityCode state
      userSafeReason remediation
      latestAttemptAt latestSuccessAt freshnessAt
      records { read created updated skipped failed }
      outputs { code label count }
      validation { state code message }
      actions { kind permitted processKey requiredCapability disabledCode disabledReason }
      xeroPipelineAvailable
    }
  }
}
'''

# src/v2/api/ingest.js
LIVE_XERO_PIPELINE = '''
query LiveXeroPipeline($context: FinancialContextInput!) {
  xeroPipelineSummary(context: $context) {
    context { entityId financialYear fiscalYearStartMonth selectionMode selectedPeriods }
    state completionPercent attentionCount
    firstBlocker { userSafeReason }
    stages {
      key processKey label order category required state latestRunId
      latestAttemptAt latestSuccessAt
      records { read created updated skipped failed }
      outputs { code label count }
      validation { state code message }
      prerequisites { code satisfied message }
      blocker { userSafeReason }
      nextValidAction
    }
  }
}
'''

# src/v2/api/sourceConnections.js
SOURCE_CONNECTIONS = '''
query SourceConnections($context: FinancialContextInput!) {
  sourceConnections(context: $context) {
    resolvedContext {
      entityId financialYear fiscalYearStartMonth startsOn endsOn selectionMode selectedPeriods
    }
    checkedAt
    summary { total active needsSetup readyDestinations }
    connections {
      key displayName category configurationState readinessState availabilityCode
      userSafeReason sourceEvidenceCount sourceEvidenceAt lastSuccessfulRunAt
      validationState latestV2RunState safeIdentity
      actions { kind permitted reason requiredCapability expectedState }
    }
  }
}
'''

CONTEXT_SCOPED = {
    'LiveOverviewSources': LIVE_OVERVIEW_SOURCES,
    'LiveXeroPipeline': LIVE_XERO_PIPELINE,
    'SourceConnections': SOURCE_CONNECTIONS,
}

# FY2026 for an entity whose fiscal year starts in March.
MARCH_FY2026 = [
    '2025-03', '2025-04', '2025-05', '2025-06', '2025-07', '2025-08',
    '2025-09', '2025-10', '2025-11', '2025-12', '2026-01', '2026-02',
]


class BrowserEndToEndTests(TestCase):
    """Sign in as a browser would, then drive the real V2 documents."""

    def setUp(self):
        self.graphql_url = reverse('web_api_v2:graphql')
        self.login_url = reverse('web_api_v2_auth:login')
        self.refresh_url = reverse('web_api_v2_auth:refresh')
        self.logout_url = reverse('web_api_v2_auth:logout')

        self.password = 'safe-e2e-pass-4417'
        self.user = get_user_model().objects.create_user(
            username='e2e-reviewer',
            email='e2e-reviewer@example.com',
            password=self.password,
            first_name='End',
            last_name='ToEnd',
        )
        self.entity = XeroTenant.objects.create(
            tenant_id='e2e-march-entity',
            tenant_name='March Year End (Pty) Ltd',
            fiscal_year_start_month=3,
        )
        self.foreign_entity = XeroTenant.objects.create(
            tenant_id='e2e-foreign-entity',
            tenant_name='Not Mine (Pty) Ltd',
            fiscal_year_start_month=3,
        )
        UserEntityMembership.objects.create(
            user=self.user,
            entity=self.entity,
            role=UserEntityMembership.Role.VIEWER,
            active=True,
        )

    # -- helpers ---------------------------------------------------------

    def sign_in(self):
        response = self.client.post(
            self.login_url,
            data=json.dumps({'username': self.user.username, 'password': self.password}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def graphql(self, access, query, variables=None, operation_name=None):
        payload = {'query': query}
        if variables is not None:
            payload['variables'] = variables
        if operation_name is not None:
            payload['operationName'] = operation_name
        return self.client.post(
            self.graphql_url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {access}',
        )

    def context_input(self, entity_id=None, mode='ALL', months=None):
        selection = {'mode': mode}
        if months is not None:
            selection['months'] = months
        return {
            'entityId': entity_id or self.entity.pk,
            'financialYear': 2026,
            'periodSelection': selection,
        }

    # -- the path a browser actually takes -------------------------------

    def test_sign_in_then_read_every_live_document(self):
        session = self.sign_in()
        access = session['tokens']['access']

        viewer = self.graphql(access, VIEWER_CONTEXT, operation_name='ViewerContext')
        self.assertEqual(viewer.status_code, 200, viewer.content)
        body = viewer.json()
        self.assertNotIn('errors', body, body)
        context = body['data']['viewerContext']
        self.assertEqual(context['entitySelectionState'], 'READY')
        self.assertEqual([entity['id'] for entity in context['entities']], [str(self.entity.pk)])
        self.assertIn('VIEW_FINANCIALS', context['entities'][0]['capabilities'])
        # A read grant must never imply an execution grant.
        self.assertNotIn('RUN_INGESTION_PROCESS', context['entities'][0]['capabilities'])

        for operation_name, document in CONTEXT_SCOPED.items():
            with self.subTest(operation=operation_name):
                response = self.graphql(
                    access, document,
                    variables={'context': self.context_input()},
                    operation_name=operation_name,
                )
                self.assertEqual(response.status_code, 200, response.content)
                payload = response.json()
                self.assertNotIn('errors', payload, payload)
                self.assertIsNotNone(payload['data'])

    def test_server_resolves_the_entity_fiscal_window_not_a_july_default(self):
        """The regression guard for the hardcoded July-June frontend window."""
        access = self.sign_in()['tokens']['access']

        for operation_name, document in CONTEXT_SCOPED.items():
            with self.subTest(operation=operation_name):
                response = self.graphql(
                    access, document,
                    variables={'context': self.context_input()},
                    operation_name=operation_name,
                )
                payload = response.json()
                self.assertNotIn('errors', payload, payload)
                data = payload['data'][next(iter(payload['data']))]
                resolved = data.get('context') or data.get('resolvedContext')
                self.assertEqual(resolved['fiscalYearStartMonth'], 3)
                self.assertEqual(resolved['selectedPeriods'], MARCH_FY2026)

    def test_a_month_inside_the_entity_window_is_accepted(self):
        access = self.sign_in()['tokens']['access']
        response = self.graphql(
            access, LIVE_XERO_PIPELINE,
            variables={'context': self.context_input(mode='MONTHS', months=['2026-02'])},
            operation_name='LiveXeroPipeline',
        )
        payload = response.json()
        self.assertNotIn('errors', payload, payload)
        resolved = payload['data']['xeroPipelineSummary']['context']
        self.assertEqual(resolved['selectionMode'], 'MONTHS')
        self.assertEqual(resolved['selectedPeriods'], ['2026-02'])

    def test_a_month_outside_the_entity_window_is_refused(self):
        """2026-06 is inside a July-June year and outside a March one."""
        access = self.sign_in()['tokens']['access']
        response = self.graphql(
            access, LIVE_XERO_PIPELINE,
            variables={'context': self.context_input(mode='MONTHS', months=['2026-06'])},
            operation_name='LiveXeroPipeline',
        )
        payload = response.json()
        self.assertIn('errors', payload)
        self.assertEqual(payload['errors'][0]['extensions']['code'], 'VALIDATION_ERROR')

    def test_a_foreign_entity_is_refused_without_leaking_its_existence(self):
        access = self.sign_in()['tokens']['access']
        for operation_name, document in CONTEXT_SCOPED.items():
            with self.subTest(operation=operation_name):
                response = self.graphql(
                    access, document,
                    variables={'context': self.context_input(entity_id=self.foreign_entity.pk)},
                    operation_name=operation_name,
                )
                payload = response.json()
                self.assertIn('errors', payload, payload)
                self.assertEqual(payload['errors'][0]['extensions']['code'], 'FORBIDDEN_ENTITY')
                self.assertNotIn(self.foreign_entity.tenant_name, json.dumps(payload))

    def test_an_unknown_entity_is_refused_the_same_way_as_a_foreign_one(self):
        access = self.sign_in()['tokens']['access']
        response = self.graphql(
            access, VIEWER_CONTEXT.replace('ViewerContext', 'ViewerContext'),
            operation_name='ViewerContext',
        )
        self.assertEqual(response.status_code, 200)

        foreign = self.graphql(
            access, SOURCE_CONNECTIONS,
            variables={'context': self.context_input(entity_id='does-not-exist')},
            operation_name='SourceConnections',
        )
        unknown_code = foreign.json()['errors'][0]['extensions']['code']

        mine = self.graphql(
            access, SOURCE_CONNECTIONS,
            variables={'context': self.context_input(entity_id=self.foreign_entity.pk)},
            operation_name='SourceConnections',
        )
        self.assertEqual(unknown_code, mine.json()['errors'][0]['extensions']['code'])

    def test_graphql_refuses_an_unauthenticated_browser(self):
        response = self.client.post(
            self.graphql_url,
            data=json.dumps({'query': VIEWER_CONTEXT}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['errors'][0]['extensions']['code'], 'UNAUTHENTICATED')

    def test_the_refresh_and_logout_lifecycle_a_browser_relies_on(self):
        session = self.sign_in()

        refreshed = self.client.post(
            self.refresh_url,
            data=json.dumps({'refresh': session['tokens']['refresh']}),
            content_type='application/json',
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.content)
        rotated = refreshed.json()
        # The client stores both halves; a response missing either one would
        # silently sign the user out on the next request.
        self.assertIn('access', rotated)
        self.assertIn('refresh', rotated)

        viewer = self.graphql(rotated['access'], VIEWER_CONTEXT, operation_name='ViewerContext')
        self.assertEqual(viewer.status_code, 200, viewer.content)

        logout = self.client.post(
            self.logout_url,
            data=json.dumps({'refresh': rotated['refresh']}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {rotated["access"]}',
        )
        self.assertEqual(logout.status_code, 200, logout.content)
        self.assertTrue(logout.json()['refreshTokenRevoked'])

        replayed = self.client.post(
            self.refresh_url,
            data=json.dumps({'refresh': rotated['refresh']}),
            content_type='application/json',
        )
        self.assertEqual(replayed.status_code, 401)


class BrowserEndToEndWithoutMembershipTests(TestCase):
    """Authentication and entity access are separate controls.

    The 2026-08-22 incident turned on exactly this: sign-in worked, so the
    absence of any membership row looked like a backend outage instead of
    unprovisioned access.
    """

    def setUp(self):
        self.graphql_url = reverse('web_api_v2:graphql')
        self.login_url = reverse('web_api_v2_auth:login')
        self.password = 'safe-e2e-pass-8823'
        self.user = get_user_model().objects.create_user(
            username='e2e-unprovisioned', password=self.password,
        )
        XeroTenant.objects.create(tenant_id='e2e-unseen', tenant_name='Unseen (Pty) Ltd')

    def test_sign_in_succeeds_but_reports_no_accessible_entities(self):
        response = self.client.post(
            self.login_url,
            data=json.dumps({'username': self.user.username, 'password': self.password}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)

        viewer = self.client.post(
            self.graphql_url,
            data=json.dumps({'query': VIEWER_CONTEXT}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {response.json()["tokens"]["access"]}',
        )
        payload = viewer.json()
        self.assertNotIn('errors', payload, payload)
        context = payload['data']['viewerContext']
        self.assertEqual(context['entitySelectionState'], 'NO_ACCESSIBLE_ENTITIES')
        self.assertEqual(context['entities'], [])
