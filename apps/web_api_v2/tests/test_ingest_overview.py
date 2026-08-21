import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.web_api_v2.models import (
    IngestProcessRun,
    IngestSourceJobDefinition,
    UserEntityCapability,
    UserEntityMembership,
)
from apps.xero.xero_core.models import XeroTenant


QUERY = '''
query IngestOverview($entityId: ID!, $periods: [YearMonth!]!) {
  ingestOverview(input: {entityId: $entityId, periods: $periods}) {
    entityId selectedPeriods attentionCount stageState completionPercent
    sourceJobDefinitions {
      id key sourceFamily label required connectionState
      supportedOperations readCapabilities
      periodRunSummaries {
        period state latestAttemptAt latestSuccessAt freshnessAt
        records { read created updated skipped failed }
        outputs
        validation { state code message }
        prerequisites { code satisfied message }
        blockedReason { code message }
        nextValidAction
      }
    }
  }
}
'''


class IngestOverviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='reader', password='safe-test')
        self.other = get_user_model().objects.create_user(username='other-reader', password='safe-test')
        self.entity = XeroTenant.objects.create(tenant_id='overview-a', tenant_name='Overview A')
        self.other_entity = XeroTenant.objects.create(
            tenant_id='overview-b', tenant_name='Overview B',
        )
        self.membership = UserEntityMembership.objects.create(user=self.user, entity=self.entity)
        UserEntityMembership.objects.create(user=self.other, entity=self.other_entity)
        self.url = reverse('web_api_v2:graphql')
        self.token = str(AccessToken.for_user(self.user))

    def _query(self, entity=None, periods=None):
        return self.client.post(
            self.url,
            data=json.dumps({
                'query': QUERY,
                'operationName': 'IngestOverview',
                'variables': {
                    'entityId': (entity or self.entity).pk,
                    'periods': periods or ['2026-07'],
                },
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

    def _overview(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('errors', response.json())
        return response.json()['data']['ingestOverview']

    @patch('apps.web_api_v2.queries.ingest_overview.has_tenant_credentials', return_value=True)
    @patch('apps.web_api_v2.queries.ingest_overview.prerequisite_status', return_value=[])
    def test_persistent_catalogue_renders_without_period_runs(self, prerequisites, credentials):
        overview = self._overview(self._query(periods=['2026-08', '2026-07']))
        self.assertEqual(overview['selectedPeriods'], ['2026-07', '2026-08'])
        self.assertEqual(len(overview['sourceJobDefinitions']), 8)
        self.assertEqual(IngestSourceJobDefinition.objects.filter(entity=self.entity).count(), 8)
        self.assertEqual(
            {summary['state'] for definition in overview['sourceJobDefinitions']
             for summary in definition['periodRunSummaries']},
            {'NOT_RUN'},
        )
        self.assertEqual(overview['completionPercent'], 0)
        self.assertEqual(overview['stageState'], 'ATTENTION_REQUIRED')
        self.assertEqual(overview['attentionCount'], 5)

    def test_cross_entity_catalogue_is_denied_before_reads(self):
        response = self._query(entity=self.other_entity)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['errors'][0]['extensions']['code'], 'FORBIDDEN_ENTITY')
        self.assertIsNone(response.json()['data'])

    @patch('apps.web_api_v2.queries.ingest_overview.has_tenant_credentials', return_value=True)
    @patch('apps.web_api_v2.queries.ingest_overview.prerequisite_status', return_value=[])
    def test_execution_next_action_is_deny_by_default_then_explicit(self, prerequisites, credentials):
        overview = self._overview(self._query())
        self.assertTrue(all(
            summary['nextValidAction'] == 'NONE'
            for definition in overview['sourceJobDefinitions']
            for summary in definition['periodRunSummaries']
        ))
        UserEntityCapability.objects.create(
            membership=self.membership,
            code=UserEntityCapability.Code.RUN_INGESTION_PROCESS,
            granted_by=self.user,
        )
        overview = self._overview(self._query())
        self.assertTrue(all(
            summary['nextValidAction'] == 'RUN'
            for definition in overview['sourceJobDefinitions']
            for summary in definition['periodRunSummaries']
        ))

    @patch('apps.web_api_v2.queries.ingest_overview.has_tenant_credentials', return_value=True)
    @patch('apps.web_api_v2.queries.ingest_overview.prerequisite_status', return_value=[])
    def test_no_period_data_is_distinct_from_not_run(self, prerequisites, credentials):
        IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key='metadata',
            state=IngestProcessRun.State.SUCCEEDED,
            idempotency_key='overview-run-1',
            request_fingerprint='a' * 64,
            periods=['2026-06'],
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        overview = self._overview(self._query(periods=['2026-07']))
        metadata = next(item for item in overview['sourceJobDefinitions'] if item['key'] == 'metadata')
        self.assertEqual(metadata['periodRunSummaries'][0]['state'], 'NO_PERIOD_DATA')
        invoices = next(item for item in overview['sourceJobDefinitions'] if item['key'] == 'invoice-sync')
        self.assertEqual(invoices['periodRunSummaries'][0]['state'], 'NOT_RUN')

    def test_not_configured_and_temporarily_unavailable_are_explicit(self):
        definition = IngestSourceJobDefinition.objects.get(entity=self.entity, key='metadata')
        definition.configuration_state = IngestSourceJobDefinition.ConfigurationState.NOT_CONFIGURED
        definition.save(update_fields=('configuration_state', 'updated_at'))
        overview = self._overview(self._query())
        metadata = next(item for item in overview['sourceJobDefinitions'] if item['key'] == 'metadata')
        self.assertEqual(metadata['connectionState'], 'NOT_CONFIGURED')
        self.assertEqual(metadata['periodRunSummaries'][0]['state'], 'NOT_CONFIGURED')
        self.assertLess(overview['completionPercent'], 100)

        definition.configuration_state = IngestSourceJobDefinition.ConfigurationState.CONFIGURED
        definition.save(update_fields=('configuration_state', 'updated_at'))
        self.entity.reauth_required = True
        self.entity.save(update_fields=('reauth_required',))
        overview = self._overview(self._query())
        metadata = next(item for item in overview['sourceJobDefinitions'] if item['key'] == 'metadata')
        self.assertEqual(metadata['connectionState'], 'TEMPORARILY_UNAVAILABLE')
        self.assertEqual(
            metadata['periodRunSummaries'][0]['state'], 'TEMPORARILY_UNAVAILABLE',
        )
        self.assertGreater(overview['attentionCount'], 0)
        self.assertLess(overview['completionPercent'], 100)

    @patch('apps.web_api_v2.queries.ingest_overview.has_tenant_credentials', return_value=True)
    @patch('apps.web_api_v2.queries.ingest_overview.prerequisite_status', return_value=[])
    def test_completion_reaches_100_only_when_all_required_jobs_validate(self, prerequisites, credentials):
        for definition in IngestSourceJobDefinition.objects.filter(entity=self.entity, required=True):
            IngestProcessRun.objects.create(
                entity=self.entity,
                actor=self.user,
                process_key=definition.key,
                state=IngestProcessRun.State.SUCCEEDED,
                idempotency_key=f'complete-{definition.key}',
                request_fingerprint='b' * 64,
                periods=['2026-07'],
                started_at=timezone.now(),
                finished_at=timezone.now(),
            )
        overview = self._overview(self._query())
        self.assertEqual(overview['completionPercent'], 100)
        self.assertEqual(overview['stageState'], 'COMPLETE')
        self.assertEqual(overview['attentionCount'], 0)

        required = IngestSourceJobDefinition.objects.filter(entity=self.entity, required=True).first()
        required.configuration_state = IngestSourceJobDefinition.ConfigurationState.NOT_CONFIGURED
        required.save(update_fields=('configuration_state', 'updated_at'))
        overview = self._overview(self._query())
        self.assertLess(overview['completionPercent'], 100)
        self.assertEqual(overview['stageState'], 'ATTENTION_REQUIRED')
