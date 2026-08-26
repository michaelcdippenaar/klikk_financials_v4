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
from apps.web_api_v2.services import xero_connection_status as status_service
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_sync.models import XeroLastUpdate


XERO_CONNECTION_STATUS_QUERY = '''
query XeroConnectionStatus($context: FinancialContextInput!) {
  xeroConnectionStatus(context: $context) {
    resolvedContext {
      entityId financialYear fiscalYearStartMonth startsOn endsOn
      selectionMode selectedPeriods
    }
    configured
    authorizationState
    tokenActionRequired
    sourceEvidenceAt
    lastSuccessfulRunAt
    availabilityCode
    userSafeReason
    actions { kind permitted reason requiredCapability expectedState }
  }
}
'''


class XeroConnectionStatusTests(TestCase):
    def setUp(self):
        self.url = reverse('web_api_v2:graphql')
        self.user = get_user_model().objects.create_user(username='xero-status-viewer')
        self.other_user = get_user_model().objects.create_user(username='other-viewer')
        self.no_access_user = get_user_model().objects.create_user(username='no-access')
        self.entity = XeroTenant.objects.create(
            tenant_id='xero-status-entity',
            tenant_name='Xero Status Entity',
            fiscal_year_start_month=7,
        )
        self.other_entity = XeroTenant.objects.create(
            tenant_id='foreign-xero-status-entity',
            tenant_name='Xero Status Entity',
            fiscal_year_start_month=7,
        )
        self.inactive_entity = XeroTenant.objects.create(
            tenant_id='inactive-xero-status-entity',
            tenant_name='Inactive Xero Status Entity',
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
                'query': XERO_CONNECTION_STATUS_QUERY,
                'variables': {'context': context or self._context()},
                'operationName': 'XeroConnectionStatus',
            }),
            content_type='application/json',
            **headers,
        )

    def _data(self, response):
        body = response.json()
        self.assertNotIn('errors', body)
        return body['data']['xeroConnectionStatus']

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

    def test_configured_measured_zero_is_available_without_run_or_freshness(self):
        self._configure_xero()
        with CaptureQueriesContext(connection) as captured:
            data = self._data(self._post(self.user))

        self.assertEqual(data['resolvedContext']['entityId'], self.entity.pk)
        self.assertEqual(data['resolvedContext']['selectedPeriods'], ['2025-07'])
        self.assertTrue(data['configured'])
        self.assertEqual(data['authorizationState'], 'AUTHORIZED')
        self.assertFalse(data['tokenActionRequired'])
        self.assertEqual(data['availabilityCode'], 'AVAILABLE')
        self.assertIsNone(data['sourceEvidenceAt'])
        self.assertIsNone(data['lastSuccessfulRunAt'])
        self.assertTrue(all(not action['permitted'] for action in data['actions']))
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

    def test_source_and_v2_run_evidence_remain_distinct(self):
        self._configure_xero()
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
            idempotency_key='xero-status-run',
            request_fingerprint='2' * 64,
            periods=['2025-07'],
            finished_at=timezone.now() - datetime.timedelta(hours=1),
        )
        IngestProcessRunPeriod.objects.create(run=run, period='2025-07')

        data = self._data(self._post(self.user))

        self.assertEqual(
            datetime.datetime.fromisoformat(data['sourceEvidenceAt']),
            source_evidence_at,
        )
        self.assertEqual(
            datetime.datetime.fromisoformat(data['lastSuccessfulRunAt']),
            run.finished_at,
        )
        self.assertNotEqual(data['sourceEvidenceAt'], data['lastSuccessfulRunAt'])

    def test_missing_token_or_job_is_typed_not_configured(self):
        missing_all = self._data(self._post(self.user))
        self.assertFalse(missing_all['configured'])
        self.assertEqual(missing_all['authorizationState'], 'NOT_CONFIGURED')
        self.assertFalse(missing_all['tokenActionRequired'])
        self.assertEqual(missing_all['availabilityCode'], 'NOT_CONFIGURED')
        self.assertIsNone(missing_all['sourceEvidenceAt'])
        self.assertIsNone(missing_all['lastSuccessfulRunAt'])

        XeroClientCredentials.objects.create(
            user=self.user,
            client_id='synthetic-client-id',
            client_secret='synthetic-client-secret',
            scope=[],
            tenant_tokens={str(self.entity.pk): {'token': {'synthetic': True}}},
        )
        IngestSourceJobDefinition.objects.filter(
            entity=self.entity,
            source_family=IngestSourceJobDefinition.SourceFamily.XERO,
        ).delete()
        missing_job = self._data(self._post(self.user))
        self.assertFalse(missing_job['configured'])
        self.assertEqual(missing_job['availabilityCode'], 'NOT_CONFIGURED')

    def test_reauthorization_is_typed_and_stored_reason_is_redacted(self):
        self._configure_xero()
        self.entity.reauth_required = True
        self.entity.reauth_reason = 'provider-secret-marker'
        self.entity.save(update_fields=('reauth_required', 'reauth_reason'))

        response = self._post(self.user)
        data = self._data(response)

        self.assertTrue(data['configured'])
        self.assertEqual(data['authorizationState'], 'REAUTHORIZATION_REQUIRED')
        self.assertTrue(data['tokenActionRequired'])
        self.assertEqual(data['availabilityCode'], 'UNAVAILABLE')
        self.assertIsNone(data['sourceEvidenceAt'])
        self.assertIsNone(data['lastSuccessfulRunAt'])
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
            username='inactive-xero-status-viewer',
            is_active=False,
        )
        UserEntityMembership.objects.create(user=inactive_user, entity=self.entity)

        response = self._post(inactive_user)

        self.assertEqual(response.status_code, 401)

    @patch(
        'apps.web_api_v2.queries.xero_connection_status.capability_codes_for_membership',
        return_value=(),
    )
    def test_view_financials_is_required(self, capabilities):
        response = self._post(self.user)
        error = response.json()['errors'][0]
        self.assertEqual(error['extensions']['code'], 'PERMISSION_DENIED')
        self.assertFalse(error['extensions']['retryable'])
        self.assertEqual(
            error['extensions']['userSafeReason'],
            'You do not have permission to view Xero connection status.',
        )

    @patch(
        'apps.web_api_v2.schema.build_xero_connection_status',
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
        source = inspect.getsource(status_service)
        for forbidden in (
            'XeroApiClient',
            'requests.',
            '/xero/',
            'client_secret',
            'refresh_token',
        ):
            self.assertNotIn(forbidden, source)
