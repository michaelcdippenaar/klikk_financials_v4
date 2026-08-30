from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase


User = get_user_model()


class LoginViewTests(APITestCase):
    url = '/api/auth/login/'

    def create_user(self, username, email, password, **extra):
        return User.objects.create_user(
            username=username,
            email=email,
            password=password,
            **extra,
        )

    def test_duplicate_email_logs_in_account_matching_password(self):
        self.create_user('first-user', 'shared@example.com', 'first-password')
        expected_user = self.create_user(
            'second-user',
            'shared@example.com',
            'second-password',
        )

        response = self.client.post(
            self.url,
            {'username': 'SHARED@example.com', 'password': 'second-password'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], expected_user.id)
        self.assertIn('access', response.data['tokens'])
        self.assertIn('refresh', response.data['tokens'])

    def test_duplicate_email_with_shared_password_requests_username(self):
        self.create_user('first-user', 'shared@example.com', 'same-password')
        self.create_user('second-user', 'shared@example.com', 'same-password')

        response = self.client.post(
            self.url,
            {'username': 'shared@example.com', 'password': 'same-password'},
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data['error'],
            'Multiple accounts use this email. Sign in with your username.',
        )

    def test_duplicate_email_with_wrong_password_is_invalid_credentials(self):
        self.create_user('first-user', 'shared@example.com', 'first-password')
        self.create_user('second-user', 'shared@example.com', 'second-password')

        response = self.client.post(
            self.url,
            {'username': 'shared@example.com', 'password': 'wrong-password'},
            format='json',
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['error'], 'Invalid credentials')

    def test_username_login_still_works_when_email_is_duplicated(self):
        expected_user = self.create_user(
            'first-user',
            'shared@example.com',
            'first-password',
        )
        self.create_user('second-user', 'shared@example.com', 'second-password')

        response = self.client.post(
            self.url,
            {'username': 'first-user', 'password': 'first-password'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], expected_user.id)

    def test_username_login_supports_usernames_containing_at_sign(self):
        expected_user = self.create_user(
            'excel@addin',
            'owner@example.com',
            'addin-password',
        )
        self.create_user(
            'owner-account',
            'excel@addin',
            'different-password',
        )

        response = self.client.post(
            self.url,
            {'username': 'excel@addin', 'password': 'addin-password'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], expected_user.id)
