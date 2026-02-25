"""
TM1 REST API client for executing TI processes and testing connections.

Credentials are resolved in order:
  1. Explicit arguments passed to the function
  2. Django settings (TM1_BASE_URL / TM1_USER / TM1_PASSWORD)
  3. Active TM1ServerConfig row in the database
"""
import requests
from requests.auth import HTTPBasicAuth
from django.conf import settings


def _get_server_config():
    """Return (base_url, user, password) from the active DB row, or (None,None,None)."""
    try:
        from apps.planning_analytics.models import TM1ServerConfig
        cfg = TM1ServerConfig.get_active()
        if cfg:
            return cfg.base_url, cfg.username, cfg.password
    except Exception:
        pass
    return None, None, None


def _resolve_credentials(base_url=None, user=None, password=None):
    """Resolve TM1 credentials from args -> settings -> DB."""
    if not base_url:
        base_url = getattr(settings, 'TM1_BASE_URL', None)
    if user is None:
        user = getattr(settings, 'TM1_USER', None)
    if password is None:
        password = getattr(settings, 'TM1_PASSWORD', None)

    if not base_url:
        db_url, db_user, db_pw = _get_server_config()
        base_url = base_url or db_url
        if user is None:
            user = db_user
        if password is None:
            password = db_pw

    return base_url, user, password


def _build_auth(user, password):
    if user is not None and user != '':
        return HTTPBasicAuth(user, password or '')
    return None


def execute_process(process_name, parameters=None, base_url=None, user=None, password=None):
    """Execute a TM1 TI process via the REST API."""
    base_url, user, password = _resolve_credentials(base_url, user, password)

    if not base_url:
        return {
            'success': False,
            'message': 'TM1 base URL is not configured (settings, DB, or request).',
        }

    url = f"{base_url.rstrip('/')}/Processes('{process_name}')/tm1.Execute"
    auth = _build_auth(user, password)
    timeout = getattr(settings, 'TM1_REQUEST_TIMEOUT', 300)
    verify = getattr(settings, 'TM1_VERIFY_SSL', False)

    body = {}
    if parameters:
        body['Parameters'] = [
            {'Name': k, 'Value': str(v)} for k, v in parameters.items()
        ]

    try:
        resp = requests.post(url, json=body, auth=auth, timeout=timeout, verify=verify)
        if resp.status_code in (200, 204):
            return {
                'success': True,
                'message': f"Process '{process_name}' executed successfully",
                'detail': {
                    'status_code': resp.status_code,
                    'note': "TM1 reports 'started'. Check TM1 Process Monitor for actual outcome.",
                },
            }
        return {
            'success': False,
            'message': f"TM1 returned status {resp.status_code}",
            'detail': {'status_code': resp.status_code, 'body': resp.text[:500]},
        }
    except requests.exceptions.Timeout:
        return {'success': False, 'message': f"TM1 request timed out after {timeout}s"}
    except requests.exceptions.ConnectionError as exc:
        return {'success': False, 'message': f"Cannot connect to TM1: {exc}"}
    except Exception as exc:
        return {'success': False, 'message': f"Unexpected error: {exc}"}


def test_connection(base_url=None, user=None, password=None):
    """Test connectivity to a TM1 server."""
    base_url, user, password = _resolve_credentials(base_url, user, password)

    if not base_url:
        return {'success': False, 'message': 'No TM1 base URL configured.'}

    auth = _build_auth(user, password)
    verify = getattr(settings, 'TM1_VERIFY_SSL', False)

    try:
        resp = requests.get(base_url.rstrip('/') + '/', auth=auth, timeout=15, verify=verify)
        if resp.status_code == 200:
            return {'success': True, 'message': 'Connected to TM1 successfully.'}
        return {
            'success': False,
            'message': f'TM1 responded with status {resp.status_code}',
            'detail': resp.text[:300],
        }
    except requests.exceptions.ConnectionError as exc:
        return {'success': False, 'message': f'Connection failed: {exc}'}
    except Exception as exc:
        return {'success': False, 'message': f'Error: {exc}'}
