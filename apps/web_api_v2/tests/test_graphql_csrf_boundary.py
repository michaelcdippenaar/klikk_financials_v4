import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from rest_framework_simplejwt.tokens import AccessToken


VIEWER_QUERY = """
query ViewerContext {
  viewerContext {
    user { username }
  }
}
"""

MUTATION_DOCUMENT = """
mutation UnsupportedMutation {
  unsupported
}
"""


class GraphQLCsrfBoundaryTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = reverse("web_api_v2:graphql")
        self.password = "csrf-boundary-test-password"
        self.user = get_user_model().objects.create_user(
            username="csrf-reader",
            password=self.password,
        )

    def _request(self, *, token=None, body=None, content_type="application/json"):
        headers = {}
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        payload = body
        if payload is None:
            payload = json.dumps({
                "query": VIEWER_QUERY,
                "operationName": "ViewerContext",
            }).encode()
        return self.client.generic(
            "POST",
            self.url,
            payload,
            content_type=content_type,
            **headers,
        )

    def _assert_typed_error(self, response, status_code, code):
        self.assertEqual(response.status_code, status_code)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        body = response.json()
        self.assertEqual(body["errors"][0]["extensions"]["code"], code)
        correlation_id = body["errors"][0]["extensions"]["correlationId"]
        self.assertTrue(correlation_id)
        self.assertEqual(response["X-Correlation-ID"], correlation_id)
        self.assertNotIn("CSRF verification failed", response.content.decode())

    def test_missing_jwt_bypasses_csrf_and_returns_typed_401(self):
        response = self._request()
        self._assert_typed_error(response, 401, "UNAUTHENTICATED")

    @override_settings(KLIKK_API_TOKEN="service-secret")
    def test_invalid_expired_and_service_credentials_are_typed_401(self):
        expired = AccessToken.for_user(self.user)
        expired.set_exp(lifetime=timedelta(seconds=-1))
        for credential in ("not-a-jwt", str(expired), "service-secret"):
            with self.subTest(credential=credential[:12]):
                response = self._request(token=credential)
                self._assert_typed_error(response, 401, "UNAUTHENTICATED")

    def test_valid_jwt_reaches_query_without_csrf_token(self):
        response = self._request(token=str(AccessToken.for_user(self.user)))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertEqual(
            response.json()["data"]["viewerContext"]["user"]["username"],
            self.user.username,
        )
        self.assertTrue(response["X-Correlation-ID"])

    def test_malformed_transport_inputs_are_typed_validation_errors(self):
        token = str(AccessToken.for_user(self.user))
        cases = (
            (b"", "application/json"),
            (b"{", "application/json"),
            (json.dumps([]).encode(), "application/json"),
            (json.dumps({"query": ""}).encode(), "application/json"),
            (
                json.dumps({"query": VIEWER_QUERY, "variables": []}).encode(),
                "application/json",
            ),
            (
                json.dumps({"query": VIEWER_QUERY}).encode(),
                "",
            ),
        )
        for body, content_type in cases:
            with self.subTest(body=body[:20], content_type=content_type):
                response = self._request(
                    token=token,
                    body=body,
                    content_type=content_type,
                )
                self._assert_typed_error(response, 400, "VALIDATION_ERROR")

    def test_graphql_remains_post_only_and_query_only(self):
        get_response = self.client.get(self.url)
        self.assertEqual(get_response.status_code, 405)

        response = self._request(
            token=str(AccessToken.for_user(self.user)),
            body=json.dumps({
                "query": MUTATION_DOCUMENT,
                "operationName": "UnsupportedMutation",
            }).encode(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("errors", response.json())
        self.assertNotIn("mutation", response.json().get("data") or {})

    def test_v2_login_and_refresh_remain_reachable_without_csrf(self):
        login = self.client.post(
            reverse("web_api_v2_auth:login"),
            data=json.dumps({
                "username": self.user.username,
                "password": self.password,
            }),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)
        refresh = self.client.post(
            reverse("web_api_v2_auth:refresh"),
            data=json.dumps({"refresh": login.json()["tokens"]["refresh"]}),
            content_type="application/json",
        )
        self.assertEqual(refresh.status_code, 200)
        self.assertIn("access", refresh.json())

    def test_admin_session_post_still_requires_csrf(self):
        response = self.client.post(
            reverse("admin:login"),
            data={"username": self.user.username, "password": self.password},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF verification failed", response.content.decode())
