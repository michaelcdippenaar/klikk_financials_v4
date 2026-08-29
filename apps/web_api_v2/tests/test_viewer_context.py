import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework_simplejwt.tokens import AccessToken

from apps.web_api_v2.auth import BrowserAuthenticationUnavailable
from apps.web_api_v2.models import UserEntityCapability, UserEntityMembership, ViewerPreference
from apps.xero.xero_core.models import XeroTenant


VIEWER_QUERY = '''
query ViewerContext {
  viewerContext {
    user { id username displayName email }
    entitySelectionState
    entities { id name role active status capabilities }
    preferences { defaultEntityId defaultFinancialYear }
  }
}
'''


class ViewerContextTests(TestCase):
    def setUp(self):
        self.url = reverse('web_api_v2:graphql')
        self.password = 'safe-test-pass-9274'
        self.user = get_user_model().objects.create_user(
            username='reviewer',
            email='reviewer@example.com',
            password=self.password,
            first_name='Review',
            last_name='User',
        )
        self.other_user = get_user_model().objects.create_user(
            username='other', password=self.password,
        )
        self.allowed = XeroTenant.objects.create(
            tenant_id='tenant-allowed', tenant_name='Allowed Entity',
        )
        self.inactive = XeroTenant.objects.create(
            tenant_id='tenant-inactive', tenant_name='Inactive Entity',
        )
        self.other = XeroTenant.objects.create(
            tenant_id='tenant-other', tenant_name='Other Entity',
        )
        UserEntityMembership.objects.create(
            user=self.user,
            entity=self.allowed,
            role=UserEntityMembership.Role.REVIEWER,
        )
        UserEntityMembership.objects.create(
            user=self.user, entity=self.inactive, active=False,
        )
        UserEntityMembership.objects.create(user=self.other_user, entity=self.other)

    def _post(self, token=None, query=VIEWER_QUERY, variables=None, operation_name='ViewerContext'):
        headers = {}
        if token is not None:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return self.client.post(
            self.url,
            data=json.dumps({
                'query': query,
                'variables': variables or {},
                'operationName': operation_name,
            }),
            content_type='application/json',
            **headers,
        )

    def test_missing_token_returns_401(self):
        response = self._post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()['errors'][0]['extensions']['code'], 'UNAUTHENTICATED',
        )

    def test_invalid_and_expired_tokens_return_401(self):
        self.assertEqual(self._post('not-a-jwt').status_code, 401)

        token = AccessToken.for_user(self.user)
        token.set_exp(lifetime=timedelta(seconds=-1))
        self.assertEqual(self._post(str(token)).status_code, 401)

    @override_settings(KLIKK_API_TOKEN='service-secret')
    def test_service_token_is_not_accepted(self):
        self.assertEqual(self._post('service-secret').status_code, 401)

    def test_rest_login_access_token_authenticates_graphql(self):
        login = self.client.post(
            '/api/auth/login/',
            data=json.dumps({'username': self.user.username, 'password': self.password}),
            content_type='application/json',
        )
        self.assertEqual(login.status_code, 200)
        response = self._post(login.json()['tokens']['access'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['data']['viewerContext']['user']['username'], self.user.username,
        )

    def test_rest_refresh_access_token_authenticates_graphql(self):
        login = self.client.post(
            '/api/auth/login/',
            data=json.dumps({'username': self.user.username, 'password': self.password}),
            content_type='application/json',
        )
        refresh = self.client.post(
            '/api/auth/refresh/',
            data=json.dumps({'refresh': login.json()['tokens']['refresh']}),
            content_type='application/json',
        )
        self.assertEqual(refresh.status_code, 200)
        self.assertEqual(set(refresh.json()), {'access'})
        response = self._post(refresh.json()['access'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['data']['viewerContext']['user']['username'], self.user.username,
        )

    def test_returns_only_active_memberships(self):
        response = self._post(str(AccessToken.for_user(self.user)))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn('errors', body)
        context = body['data']['viewerContext']
        self.assertEqual(context['user']['displayName'], 'Review User')
        self.assertEqual(context['entitySelectionState'], 'READY')
        self.assertEqual(context['entities'], [{
            'id': 'tenant-allowed',
            'name': 'Allowed Entity',
            'role': 'REVIEWER',
            'active': True,
            'status': 'AVAILABLE',
            'capabilities': ['VIEW_FINANCIALS'],
        }])
        self.assertNotIn('tenant-inactive', response.content.decode())
        self.assertNotIn('tenant-other', response.content.decode())

    def test_no_access_is_success_with_explicit_empty_state(self):
        user = get_user_model().objects.create_user(
            username='no-entities', password=self.password,
        )
        response = self._post(str(AccessToken.for_user(user)))
        self.assertEqual(response.status_code, 200)
        context = response.json()['data']['viewerContext']
        self.assertEqual(context['entities'], [])
        self.assertEqual(context['entitySelectionState'], 'NO_ACCESSIBLE_ENTITIES')
        self.assertIsNone(context['preferences']['defaultEntityId'])

    def test_reauthorization_status_does_not_remove_access(self):
        self.allowed.reauth_required = True
        self.allowed.save(update_fields=('reauth_required',))
        response = self._post(str(AccessToken.for_user(self.user)))
        entity = response.json()['data']['viewerContext']['entities'][0]
        self.assertEqual(entity['status'], 'REAUTHORIZATION_REQUIRED')
        self.assertTrue(entity['active'])
        self.assertEqual(entity['capabilities'], ['VIEW_FINANCIALS'])

    def test_explicit_ingest_grant_is_advertised_without_role_inference(self):
        response = self._post(str(AccessToken.for_user(self.user)))
        self.assertEqual(
            response.json()['data']['viewerContext']['entities'][0]['capabilities'],
            ['VIEW_FINANCIALS'],
        )
        membership = UserEntityMembership.objects.get(user=self.user, entity=self.allowed)
        UserEntityCapability.objects.create(
            membership=membership,
            code=UserEntityCapability.Code.RUN_INGESTION_PROCESS,
            granted_by=self.user,
        )
        response = self._post(str(AccessToken.for_user(self.user)))
        self.assertEqual(
            response.json()['data']['viewerContext']['entities'][0]['capabilities'],
            ['VIEW_FINANCIALS', 'RUN_INGESTION_PROCESS'],
        )

    def test_disallowed_default_entity_is_null(self):
        ViewerPreference.objects.create(
            user=self.user,
            default_entity=self.other,
            default_financial_year=2026,
        )
        response = self._post(str(AccessToken.for_user(self.user)))
        preferences = response.json()['data']['viewerContext']['preferences']
        self.assertEqual(
            preferences, {'defaultEntityId': None, 'defaultFinancialYear': 2026},
        )

    def test_allowed_default_entity_is_returned(self):
        ViewerPreference.objects.create(user=self.user, default_entity=self.allowed)
        response = self._post(str(AccessToken.for_user(self.user)))
        preferences = response.json()['data']['viewerContext']['preferences']
        self.assertEqual(preferences['defaultEntityId'], self.allowed.pk)

    def test_get_operations_are_disabled(self):
        self.assertEqual(self.client.get(self.url, {'query': VIEWER_QUERY}).status_code, 405)

    @override_settings(WEB_API_V2_MAX_REQUEST_BYTES=16)
    def test_request_size_limit_is_enforced(self):
        self.assertEqual(self._post(str(AccessToken.for_user(self.user))).status_code, 413)

    def test_introspection_is_disabled(self):
        response = self._post(
            str(AccessToken.for_user(self.user)),
            query='{ __schema { queryType { name } } }',
            operation_name=None,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('disabled', response.json()['errors'][0]['message'].lower())

    def test_query_complexity_limit_is_enforced(self):
        # Derive the size from the configured ceiling. Hardcoding a repetition
        # count pins the test to one particular limit, so raising the ceiling
        # silently stops the test exercising it.
        from django.conf import settings
        per_alias = 3  # viewerContext { user { id } }
        aliases = settings.WEB_API_V2_MAX_FIELD_SELECTIONS // per_alias + 1
        repeated_fields = ' '.join(
            f'context{index}: viewerContext {{ user {{ id }} }}'
            for index in range(aliases)
        )
        query = f'query TooComplex {{ {repeated_fields} }}'
        with self.assertLogs('apps.web_api_v2.schema', level='WARNING') as captured:
            response = self._post(
                str(AccessToken.for_user(self.user)),
                query=query,
                operation_name='TooComplex',
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('too complex', response.json()['errors'][0]['message'].lower())
        self.assertNotIn(query, '\n'.join(captured.output))
        self.assertNotIn('viewerContext { user', '\n'.join(captured.output))

    def test_logs_metadata_but_not_token_document_variables_or_values(self):
        token = str(AccessToken.for_user(self.user))
        variables = {'financialValue': '388.00', 'secretMarker': 'never-log-me'}
        with self.assertLogs('apps.web_api_v2.views', level='INFO') as captured:
            response = self._post(token, variables=variables)
        self.assertEqual(response.status_code, 200)
        output = '\n'.join(captured.output)
        self.assertIn('operation=ViewerContext', output)
        self.assertNotIn(token, output)
        self.assertNotIn('388.00', output)
        self.assertNotIn('never-log-me', output)
        self.assertNotIn('viewerContext {', output)

    def test_invalid_operation_name_cannot_inject_log_lines(self):
        with self.assertLogs('apps.web_api_v2.views', level='INFO') as captured:
            response = self._post(
                str(AccessToken.for_user(self.user)),
                operation_name='ViewerContext\\nforged_log=true',
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn('operation=-', '\n'.join(captured.output))
        self.assertNotIn('forged_log=true', '\n'.join(captured.output))

    def test_authentication_store_outage_is_typed_and_retryable(self):
        with patch(
            'apps.web_api_v2.views.authenticate_browser_request',
            side_effect=BrowserAuthenticationUnavailable,
        ):
            response = self._post('syntactically-irrelevant')
        self.assertEqual(response.status_code, 503)
        extensions = response.json()['errors'][0]['extensions']
        self.assertEqual(extensions['code'], 'TEMPORARILY_UNAVAILABLE')
        self.assertTrue(extensions['retryable'])
        self.assertIn('correlationId', extensions)

    def test_viewer_database_outage_is_typed_without_leaking_details(self):
        with patch(
            'apps.web_api_v2.schema.build_viewer_context',
            side_effect=DatabaseError('secret SQL detail'),
        ):
            response = self._post(str(AccessToken.for_user(self.user)))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        extensions = body['errors'][0]['extensions']
        self.assertEqual(extensions['code'], 'TEMPORARILY_UNAVAILABLE')
        self.assertTrue(extensions['retryable'])
        self.assertNotIn('secret SQL detail', response.content.decode())

    def test_production_schema_has_no_demo_or_mode_fields(self):
        from apps.web_api_v2.schema import schema

        rendered = schema.as_str()
        self.assertNotIn('isDemo', rendered)
        self.assertNotIn('demoMode', rendered)
        self.assertNotIn('sourceMode', rendered)

    def test_committed_schema_snapshot_is_current(self):
        call_command('export_graphql_schema', '--check')


class ViewerContextCapabilityEnumTests(TestCase):
    """A capability grant must never be able to hide the entity list.

    This is the regression test for a live outage. MANAGE_SHARE_MAPPINGS was
    granted in production before the schema had the word for it, so building the
    viewer context raised ValueError, the whole query returned INTERNAL_ERROR,
    and a signed-in user with three active memberships saw no entities and no
    stated reason. The grant was real, the memberships were real, and every
    unauthenticated smoke check still passed.
    """

    def setUp(self):
        self.url = reverse('web_api_v2:graphql')
        self.user = get_user_model().objects.create_user(
            username='mapper', password='safe-test-pass-9274',
        )
        self.entity = XeroTenant.objects.create(
            tenant_id='tenant-mapper', tenant_name='Mapper Entity',
        )
        self.membership = UserEntityMembership.objects.create(
            user=self.user, entity=self.entity,
        )

    def _entities(self):
        response = self.client.post(
            self.url,
            data=json.dumps({'query': VIEWER_QUERY, 'operationName': 'ViewerContext'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(self.user)}',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body.get('errors'), body.get('errors'))
        return body['data']['viewerContext']['entities']

    def test_share_mapping_capability_is_expressible(self):
        UserEntityCapability.objects.create(
            membership=self.membership, code='MANAGE_SHARE_MAPPINGS',
        )
        entities = self._entities()
        self.assertEqual(len(entities), 1)
        self.assertIn('MANAGE_SHARE_MAPPINGS', entities[0]['capabilities'])

    def test_unknown_capability_does_not_hide_the_entity(self):
        # The schema will always be able to lag a grant applied by hand or by an
        # earlier deploy. When it does, the entity must still be listed — the
        # user simply does not get that one power.
        UserEntityCapability.objects.create(
            membership=self.membership, code='NOT_A_REAL_CAPABILITY',
        )
        entities = self._entities()
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]['name'], 'Mapper Entity')
        self.assertNotIn('NOT_A_REAL_CAPABILITY', entities[0]['capabilities'])

    def test_unknown_capability_alongside_known_ones_keeps_the_known(self):
        UserEntityCapability.objects.create(
            membership=self.membership, code='MANAGE_SHARE_MAPPINGS',
        )
        UserEntityCapability.objects.create(
            membership=self.membership, code='NOT_A_REAL_CAPABILITY',
        )
        capabilities = self._entities()[0]['capabilities']
        self.assertIn('MANAGE_SHARE_MAPPINGS', capabilities)
        self.assertNotIn('NOT_A_REAL_CAPABILITY', capabilities)
