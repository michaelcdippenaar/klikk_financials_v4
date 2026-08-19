from django.urls import path
from apps.deployment import views

app_name = 'deployment'

# The anonymous GitHub deploy webhook was REMOVED on 2026-08-20.
#
# POST /deployment/webhook/github/ was reachable by any anonymous caller and
# reached subprocess.run(['bash', deploy_script]): verify_github_signature()
# returns True when GITHUB_WEBHOOK_SECRET is falsy, and the secret is
# present-but-empty in the container, so the signature check was a no-op
# (SECURITY-NOTE.md §3, rated Critical).
#
# apps/deployment/views.py:github_webhook still exists but is now unrouted
# and unreachable. It must NOT be re-registered without first making
# verify_github_signature fail CLOSED on a missing secret.
urlpatterns = []
