import datetime
import inspect
import json
import socket
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from apps.web_api_v2.models import (
    IngestProcessRun,
    IngestProcessRunPeriod,
    IngestSourceJobDefinition,
    UserEntityMembership,
)
from apps.web_api_v2.services import source_connections as source_connection_service
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_data.models import XeroTransactionSource
from apps.xero.xero_sync.models import XeroLastUpdate


SOURCE_CONNECTIONS_QUERY = '''
query SourceConnections($context: FinancialContextInput!) {
  sourceConnections(context: $context) {
    resolvedContext {
      entityId financialYear fiscalYearStartMonth startsOn endsOn
      selectionMode selectedPeriods
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


class SourceConnectionsTests(TestCase):
    def setUp(self):
        self.url = reverse('web_api_v2:graphql')
        self.user = get_user_model().objects.create_user(username='source-viewer')
        self.other_user = get_user_model().objects.create_user(username='other-viewer')
        self.no_access_user = get_user_model().objects.create_user(username='no-access')
        self.entity = XeroTenant.objects.create(
            tenant_id='source-entity',
            tenant_name='Source Entity',
            fiscal_year_start_month=7,
        )
        self.other_entity = XeroTenant.objects.create(
            tenant_id='foreign-source-entity',
            tenant_name='Source Entity',
            fiscal_year_start_month=7,
        )
        self.inactive_entity = XeroTenant.objects.create(
            tenant_id='inactive-source-entity',
            tenant_name='Inactive Source Entity',
            fiscal_year_start_month=7,
        )
        UserEntityMembership.objects.create(user=self.user, entity=self.entity)
        UserEntityMembership.objects.create(
            user=self.other_user,
            entity=self.other_entity,
        )
        UserEntityMembership.objects.create(
            user=self.user,
            entity=self.inactive_entity,
            active=False,
        )

    def _context(self, entity=None):
        return {
            'entityId': (entity or self.entity).pk,
            'financialYear': 2026,
            'periodSelection': {
                'mode': 'MONTHS',
                'months': ['2025-07'],
            },
        }

    def _post(self, user=None, context=None):
        headers = {}
        if user is not None:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {AccessToken.for_user(user)}'
        return self.client.post(
            self.url,
            data=json.dumps({
                'query': SOURCE_CONNECTIONS_QUERY,
                'variables': {'context': context or self._context()},
                'operationName': 'SourceConnections',
            }),
            content_type='application/json',
            **headers,
        )

    def _data(self, response):
        body = response.json()
        self.assertNotIn('errors', body)
        return body['data']['sourceConnections']

    def _configure_xero(self, entity=None):
        entity = entity or self.entity
        XeroClientCredentials.objects.create(
            user=self.user,
            client_id='synthetic-client-id',
            client_secret='synthetic-client-secret',
            scope=[],
            tenant_tokens={
                str(entity.pk): {
                    'token': {'synthetic': True},
                },
            },
        )
        IngestSourceJobDefinition.objects.update_or_create(
            entity=entity,
            key='metadata',
            defaults={
                'source_family': IngestSourceJobDefinition.SourceFamily.XERO,
                'label': 'Metadata',
                'configuration_state': (
                    IngestSourceJobDefinition.ConfigurationState.CONFIGURED
                ),
                'active': True,
            },
        )

    def test_configured_measured_zero_returns_six_stable_read_only_rows(self):
        self._configure_xero()
        with CaptureQueriesContext(connection) as captured:
            data = self._data(self._post(self.user))

        self.assertEqual(data['resolvedContext']['entityId'], self.entity.pk)
        self.assertEqual(data['resolvedContext']['selectedPeriods'], ['2025-07'])
        self.assertEqual(
            [row['key'] for row in data['connections']],
            [
                'XERO',
                'INVESTEC_SHARE_TRADING',
                'WHATSAPP_RECEIPTS',
                'EMAIL_RECEIPTS',
                'PLANNING_ANALYTICS',
                'EXCEL_ADD_IN',
            ],
        )
        self.assertEqual(
            data['summary'],
            {'total': 6, 'active': 1, 'needsSetup': 0, 'readyDestinations': 0},
        )
        xero = data['connections'][0]
        self.assertEqual(xero['configurationState'], 'CONFIGURED')
        self.assertEqual(xero['readinessState'], 'EMPTY')
        self.assertEqual(xero['availabilityCode'], 'AVAILABLE')
        self.assertEqual(xero['sourceEvidenceCount'], 0)
        self.assertIsNone(xero['sourceEvidenceAt'])
        self.assertIsNone(xero['lastSuccessfulRunAt'])
        self.assertEqual(xero['validationState'], 'UNAVAILABLE')
        self.assertTrue(all(not action['permitted'] for action in xero['actions']))
        for row in data['connections'][1:]:
            self.assertEqual(row['availabilityCode'], 'UNAVAILABLE')
            self.assertIsNone(row['sourceEvidenceCount'])
            self.assertIsNone(row['sourceEvidenceAt'])
            self.assertTrue(all(not action['permitted'] for action in row['actions']))
        self.assertLessEqual(len(captured), 12)
        sql = '\n'.join(item['sql'] for item in captured.captured_queries)
        self.assertNotIn('client_secret', sql.lower())
        self.assertNotIn('refresh_token', sql.lower())
        credential_queries = [
            item['sql'].lower()
            for item in captured.captured_queries
            if 'xero_auth_xeroclientcredentials' in item['sql'].lower()
        ]
        self.assertEqual(len(credential_queries), 1)
        selected_columns = credential_queries[0].split(' from ', 1)[0]
        self.assertNotIn('tenant_tokens', selected_columns)
        self.assertIn('select 1 as', selected_columns)

    def test_ready_source_validation_and_v2_run_evidence_remain_distinct(self):
        self._configure_xero()
        XeroTransactionSource.objects.create(
            organisation=self.entity,
            transactions_id='xero-source-row',
            transaction_source='Invoice',
        )
        source_evidence_at = timezone.now() - datetime.timedelta(hours=3)
        XeroLastUpdate.objects.create(
            organisation=self.entity,
            end_point='journals',
            date=source_evidence_at,
        )
        run = IngestProcessRun.objects.create(
            entity=self.entity,
            actor=self.user,
            process_key='metadata',
            state=IngestProcessRun.State.SUCCEEDED,
            idempotency_key='source-connections-run',
            request_fingerprint='1' * 64,
            periods=['2025-07'],
            records_summary={'read': 99},
            finished_at=timezone.now() - datetime.timedelta(hours=1),
        )
        IngestProcessRunPeriod.objects.create(run=run, period='2025-07')

        xero = self._data(self._post(self.user))['connections'][0]

        self.assertEqual(xero['readinessState'], 'READY')
        self.assertEqual(xero['sourceEvidenceCount'], 1)
        self.assertEqual(
            datetime.datetime.fromisoformat(xero['sourceEvidenceAt']),
            source_evidence_at,
        )
        self.assertEqual(
            datetime.datetime.fromisoformat(xero['lastSuccessfulRunAt']),
            run.finished_at,
        )
        self.assertEqual(xero['latestV2RunState'], 'SUCCEEDED')
        self.assertEqual(xero['validationState'], 'UNAVAILABLE')
        self.assertNotEqual(xero['sourceEvidenceCount'], run.records_summary['read'])

    def test_missing_safe_configuration_is_typed_not_configured(self):
        data = self._data(self._post(self.user))
        xero = data['connections'][0]
        self.assertEqual(xero['configurationState'], 'NOT_CONFIGURED')
        self.assertEqual(xero['readinessState'], 'NOT_CONFIGURED')
        self.assertEqual(xero['availabilityCode'], 'NOT_CONFIGURED')
        self.assertIsNone(xero['sourceEvidenceCount'])
        self.assertEqual(data['summary']['needsSetup'], 1)

    def test_reauthorization_is_safe_unavailable_without_stored_reason(self):
        self._configure_xero()
        self.entity.reauth_required = True
        self.entity.reauth_reason = 'provider-secret-marker'
        self.entity.save(update_fields=('reauth_required', 'reauth_reason'))

        response = self._post(self.user)
        xero = self._data(response)['connections'][0]

        self.assertEqual(xero['configurationState'], 'CONFIGURED')
        self.assertEqual(xero['readinessState'], 'UNAVAILABLE')
        self.assertIsNone(xero['sourceEvidenceCount'])
        self.assertNotIn('provider-secret-marker', response.content.decode())

    def test_anonymous_and_unauthorized_entities_fail_closed(self):
        self.assertEqual(self._post().status_code, 401)
        cases = (
            (self.no_access_user, self.entity),
            (self.user, self.inactive_entity),
            (self.user, self.other_entity),
        )
        for user, entity in cases:
            response = self._post(user, self._context(entity))
            error = response.json()['errors'][0]
            self.assertEqual(error['extensions']['code'], 'FORBIDDEN_ENTITY')
            self.assertNotIn(entity.tenant_name, response.content.decode())

    def test_inactive_principal_is_rejected_before_resolution(self):
        inactive_user = get_user_model().objects.create_user(
            username='inactive-source-viewer',
            is_active=False,
        )
        UserEntityMembership.objects.create(user=inactive_user, entity=self.entity)

        response = self._post(inactive_user)

        self.assertEqual(response.status_code, 401)

    @patch(
        'apps.web_api_v2.queries.source_connections.capability_codes_for_membership',
        return_value=(),
    )
    def test_view_financials_is_required(self, capabilities):
        response = self._post(self.user)
        error = response.json()['errors'][0]
        self.assertEqual(error['extensions']['code'], 'PERMISSION_DENIED')
        self.assertFalse(error['extensions']['retryable'])
        self.assertEqual(
            error['extensions']['userSafeReason'],
            'You do not have permission to view financial source connections.',
        )

    @patch(
        'apps.web_api_v2.schema.build_source_connections',
        side_effect=DatabaseError('secret database detail'),
    )
    def test_database_failure_is_typed_and_redacted(self, build):
        response = self._post(self.user)
        error = response.json()['errors'][0]
        self.assertEqual(error['extensions']['code'], 'TEMPORARILY_UNAVAILABLE')
        self.assertTrue(error['extensions']['retryable'])
        self.assertNotIn('secret database detail', response.content.decode())

    @patch.object(socket, 'create_connection', side_effect=AssertionError('network call'))
    def test_query_uses_no_provider_network_or_legacy_fallback(self, create_connection):
        self._configure_xero()
        response = self._post(self.user)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('errors', response.json())
        create_connection.assert_not_called()
        source = inspect.getsource(source_connection_service)
        for forbidden in (
            'XeroApiClient',
            'requests.',
            '/xero/',
            '/api/investec',
            'client_secret',
            'refresh_token',
        ):
            self.assertNotIn(forbidden, source)
