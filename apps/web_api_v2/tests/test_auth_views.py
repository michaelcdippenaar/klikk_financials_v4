import json
from unittest.mock import patch

from django.db import DatabaseError

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import path

from apps.web_api_v2.auth_views import LoginView, LogoutView, RefreshView, VerifyView


urlpatterns = [
    path('api/v2/auth/login/', LoginView.as_view(), name='v2-login'),
    path('api/v2/auth/refresh/', RefreshView.as_view(), name='v2-refresh'),
    path('api/v2/auth/verify/', VerifyView.as_view(), name='v2-verify'),
    path('api/v2/auth/logout/', LogoutView.as_view(), name='v2-logout'),
]


@override_settings(ROOT_URLCONF=__name__, KLIKK_API_TOKEN='service-secret')
class BrowserAuthV2Tests(TestCase):
    password = 'safe-test-pass-9274'

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='browser-user',
            email='browser@example.com',
            password=self.password,
        )

    def _post(self, path, payload, token=None):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token}'} if token else {}
        return self.client.post(
            path, data=json.dumps(payload), content_type='application/json', **headers,
        )

    def _login(self):
        response = self._post(
            '/api/v2/auth/login/',
            {'username': self.user.username, 'password': self.password},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()['tokens']

    def test_login_refresh_rotation_and_verify(self):
        tokens = self._login()
        verified = self._post('/api/v2/auth/verify/', {'token': tokens['access']})
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json(), {'valid': True})

        refreshed = self._post('/api/v2/auth/refresh/', {'refresh': tokens['refresh']})
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn('access', refreshed.json())
        self.assertIn('refresh', refreshed.json())
        self.assertNotEqual(refreshed.json()['refresh'], tokens['refresh'])
        reused = self._post('/api/v2/auth/refresh/', {'refresh': tokens['refresh']})
        self.assertEqual(reused.status_code, 401)
        self.assertEqual(reused.json()['error']['code'], 'TOKEN_INVALID')
        still_valid = self._post('/api/v2/auth/verify/', {'token': tokens['access']})
        self.assertEqual(still_valid.status_code, 200)
        self.assertEqual(still_valid.json(), {'valid': True})
        self.assertEqual(
            self._post('/api/v2/auth/verify/', {'token': refreshed.json()['access']}).status_code,
            200,
        )

    def test_invalid_requests_have_stable_typed_errors(self):
        cases = (
            ('/api/v2/auth/login/', {'username': self.user.username, 'password': 'wrong'}, 401,
             'INVALID_CREDENTIALS'),
            ('/api/v2/auth/refresh/', {'refresh': 'not-a-jwt'}, 401, 'TOKEN_INVALID'),
            ('/api/v2/auth/verify/', {'token': 'not-a-jwt'}, 401, 'TOKEN_INVALID'),
        )
        for path, payload, expected_status, code in cases:
            with self.subTest(path=path):
                response = self._post(path, payload)
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json()['error']['code'], code)
                self.assertIn('correlationId', response.json()['error'])
                self.assertEqual(
                    response['X-Correlation-ID'], response.json()['error']['correlationId'],
                )

    @patch('apps.web_api_v2.auth_views.TokenVerifySerializer.is_valid')
    def test_verify_database_outage_is_typed_and_retryable(self, is_valid):
        marker = 'database-secret-must-not-escape'
        is_valid.side_effect = DatabaseError(marker)
        with self.assertLogs('apps.web_api_v2.auth_views', level='ERROR') as captured:
            response = self._post('/api/v2/auth/verify/', {'token': 'opaque-token'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'TEMPORARILY_UNAVAILABLE')
        self.assertTrue(response.json()['error']['retryable'])
        self.assertNotIn(marker, response.content.decode())
        self.assertNotIn(marker, '\n'.join(captured.output))

    def test_service_token_is_rejected_by_protected_logout(self):
        response = self._post(
            '/api/v2/auth/logout/', {'refresh': 'not-relevant'}, token='service-secret',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'UNAUTHENTICATED')
        self.assertIn('correlationId', response.json()['error'])

    def test_logout_revokes_refresh_token(self):
        tokens = self._login()
        response = self._post(
            '/api/v2/auth/logout/', {'refresh': tokens['refresh']}, token=tokens['access'],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'refreshTokenRevoked': True,
            'accessTokenRevoked': False,
            'accessTokenValidUntilExpiry': True,
        })

        reused = self._post('/api/v2/auth/refresh/', {'refresh': tokens['refresh']})
        self.assertEqual(reused.status_code, 401)
        self.assertEqual(reused.json()['error']['code'], 'TOKEN_INVALID')

        still_valid = self._post('/api/v2/auth/verify/', {'token': tokens['access']})
        self.assertEqual(still_valid.status_code, 200)
        self.assertEqual(still_valid.json(), {'valid': True})

    def test_logout_rejects_another_users_refresh(self):
        other = get_user_model().objects.create_user(
            username='other-browser-user', password=self.password,
        )
        own = self._login()
        other_login = self._post(
            '/api/v2/auth/login/', {'username': other.username, 'password': self.password},
        ).json()['tokens']
        response = self._post(
            '/api/v2/auth/logout/', {'refresh': other_login['refresh']}, token=own['access'],
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'TOKEN_SUBJECT_MISMATCH')

    def test_logs_do_not_contain_credentials_or_tokens(self):
        marker = 'never-log-this-password-or-token'
        with self.assertLogs('apps.web_api_v2.auth_views', level='INFO') as captured:
            self._post('/api/v2/auth/login/', {'username': self.user.username, 'password': marker})
            self._post('/api/v2/auth/verify/', {'token': marker})
        rendered = '\n'.join(captured.output)
        self.assertNotIn(marker, rendered)
        self.assertNotIn(self.user.username, rendered)

    @override_settings(WEB_API_V2_AUTH_MAX_REQUEST_BYTES=8)
    def test_auth_request_size_limit_is_typed(self):
        response = self._post(
            '/api/v2/auth/login/',
            {'username': self.user.username, 'password': self.password},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()['error']['code'], 'VALIDATION_ERROR')
