"""Narrow URL surface for the isolated local V2 backend."""

from django.http import JsonResponse
from django.urls import include, path
from django.views.decorators.http import require_GET


@require_GET
def health(_request):
    return JsonResponse({"status": "ok", "mode": "local-v2-synthetic"})


urlpatterns = [
    path("health/local-v2/", health, name="local-v2-health"),
    path("api/v2/auth/", include("apps.web_api_v2.auth_urls")),
    path("api/v2/graphql/", include("apps.web_api_v2.urls")),
]
