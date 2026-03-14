import re
from urllib.parse import urljoin

import requests
from django.conf import settings
from requests.auth import HTTPBasicAuth


ALLOWED_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}
PROTECTED_READ_ONLY_VERSIONS = {'ACTUALS', 'FORECAST'}
READ_ONLY_POST_PATH_MARKERS = (
    'executemdx',
    'executesetexpression',
    'executeview',
)


def _build_auth():
    _, user, password = _resolve_credentials()
    if user is None or user == '':
        return None
    return HTTPBasicAuth(user, password or '')


def _get_server_config():
    try:
        from apps.planning_analytics.models import TM1ServerConfig
        cfg = TM1ServerConfig.get_active()
        if cfg:
            return cfg.base_url, cfg.username, cfg.password
    except Exception:
        pass
    return None, None, None


def _resolve_credentials(base_url=None, user=None, password=None):
    db_url, db_user, db_pw = _get_server_config()
    if not base_url:
        base_url = db_url or getattr(settings, 'TM1_BASE_URL', None)
    if isinstance(base_url, str):
        base_url = base_url.strip()
    if user is None:
        user = db_user if db_user is not None else getattr(settings, 'TM1_USER', None)
    if password is None:
        password = db_pw if db_pw is not None else getattr(settings, 'TM1_PASSWORD', None)
    return base_url, user, password


def _base_url():
    base_url, _, _ = _resolve_credentials()
    if not base_url:
        raise ValueError('TM1_BASE_URL is not configured.')
    if not str(base_url).startswith(('http://', 'https://')):
        raise ValueError('TM1_BASE_URL must start with http:// or https://')
    return base_url.rstrip('/') + '/'


def _sanitize_relative_path(path):
    cleaned = (path or '').strip()
    if not cleaned:
        raise ValueError('path is required.')
    return cleaned.lstrip('/')


def _iter_text_values(value):
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, (int, float, bool)):
        yield str(value)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _iter_text_values(v)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_text_values(item)


def _get_protected_versions():
    configured = getattr(settings, 'AI_AGENT_TM1_READ_ONLY_VERSIONS', None)
    if configured and isinstance(configured, (list, tuple, set)):
        return {str(v).strip().upper() for v in configured if str(v).strip()}
    return set(PROTECTED_READ_ONLY_VERSIONS)


def _detect_version_mentions(path, body, params):
    text_blobs = [path or '']
    text_blobs.extend(_iter_text_values(body))
    text_blobs.extend(_iter_text_values(params))
    haystack = ' '.join(text_blobs).upper()

    found = set()
    for version in _get_protected_versions():
        if not version:
            continue
        if re.search(rf'(^|[^A-Z0-9]){re.escape(version)}([^A-Z0-9]|$)', haystack):
            found.add(version)
    return sorted(found)


def _is_read_operation(method, path):
    normalized_method = (method or '').upper()
    if normalized_method == 'GET':
        return True
    if normalized_method != 'POST':
        return False
    normalized_path = (path or '').strip().lower()
    return any(marker in normalized_path for marker in READ_ONLY_POST_PATH_MARKERS)


def _version_policy_decision(method, path, body, params):
    detected_versions = _detect_version_mentions(path=path, body=body, params=params)
    if not detected_versions:
        return False, detected_versions
    if _is_read_operation(method=method, path=path):
        return False, detected_versions
    return True, detected_versions


def tm1_request(method, path, body=None, params=None, headers=None, tm1_user=None, tm1_password=None):
    method = (method or '').upper().strip()
    if method not in ALLOWED_METHODS:
        raise ValueError(f'Unsupported TM1 method: {method}')

    relative_path = _sanitize_relative_path(path)
    if getattr(settings, 'AI_AGENT_DISABLE_SECURITY', False):
        blocked = False
        detected_versions = _detect_version_mentions(path=relative_path, body=body, params=params)
    else:
        blocked, detected_versions = _version_policy_decision(
            method=method,
            path=relative_path,
            body=body,
            params=params,
        )
    if blocked:
        return {
            'success': False,
            'blocked': True,
            'status_code': 403,
            'message': 'Write operations to protected versions are not allowed.',
            'policy': {
                'read_only_versions': sorted(_get_protected_versions()),
                'detected_versions': detected_versions,
                'allowed': 'Read is allowed on Actuals/Forecast. Writes are blocked there.',
            },
        }

    url = urljoin(_base_url(), relative_path)
    _, user, password = _resolve_credentials(user=tm1_user, password=tm1_password)
    timeout = int(getattr(settings, 'TM1_REQUEST_TIMEOUT', 300))
    verify = bool(getattr(settings, 'TM1_VERIFY_SSL', False))

    request_headers = {'Accept': 'application/json'}
    if headers:
        request_headers.update(headers)

    try:
        response = requests.request(
            method=method,
            url=url,
            json=body,
            params=params or {},
            headers=request_headers,
            auth=HTTPBasicAuth(user, password or '') if user not in (None, '') else None,
            timeout=timeout,
            verify=verify,
        )
    except requests.exceptions.RequestException as exc:
        return {
            'success': False,
            'blocked': False,
            'status_code': 0,
            'url': url,
            'detected_versions': detected_versions,
            'message': f'TM1 request failed: {exc}',
            'response_headers': {},
            'response_body': {},
        }

    response_payload = {}
    if response.text:
        try:
            response_payload = response.json()
        except Exception:
            response_payload = {'raw_text': response.text[:4000]}

    return {
        'success': response.ok,
        'blocked': False,
        'status_code': response.status_code,
        'url': url,
        'detected_versions': detected_versions,
        'response_headers': dict(response.headers),
        'response_body': response_payload,
    }


def tm1_test_connection():
    # Use a lightweight metadata endpoint so we always send a valid relative path.
    result = tm1_request(method='GET', path='Cubes?$top=1')
    if result['success']:
        return {'success': True, 'message': 'TM1 connection successful.', 'detail': result}
    return {'success': False, 'message': 'TM1 connection failed.', 'detail': result}


def tm1_get_version():
    """
    Query TM1 server for version info. Tries root service document and
    Configuration endpoint; returns parsed version and raw response excerpts.
    """
    version_info = {
        'success': False,
        'version': None,
        'product_version': None,
        'base_url': None,
        'source': None,
        'raw_excerpt': None,
    }
    try:
        base_url = _base_url()
        version_info['base_url'] = base_url.rstrip('/')
    except Exception as e:
        version_info['error'] = str(e)
        return version_info

    # Try GET on root (service document) – some TM1 REST APIs return version here.
    result = tm1_request(method='GET', path='')
    if result.get('success') and result.get('response_body'):
        body = result.get('response_body') or {}
        if isinstance(body, dict):
            for key in ('ServerVersion', 'ProductVersion', 'Version', '@odata.context'):
                if key in body and body[key] is not None:
                    version_info['success'] = True
                    if 'version' in key.lower():
                        version_info['version'] = version_info['version'] or str(body[key])
                        version_info['product_version'] = version_info['product_version'] or str(body[key])
                    version_info['raw_excerpt'] = {k: body.get(k) for k in list(body.keys())[:15]}
                    break
            if not version_info['version'] and body:
                version_info['success'] = True
                version_info['raw_excerpt'] = {k: body.get(k) for k in list(body.keys())[:15]}
        if version_info['success']:
            version_info['source'] = 'root'

    # If root didn’t give version, try Configuration (some PA/TM1 versions expose it).
    if not version_info.get('version'):
        result2 = tm1_request(method='GET', path='Configuration')
        if result2.get('success') and result2.get('response_body'):
            body = result2.get('response_body') or {}
            if isinstance(body, dict):
                for key in ('ProductVersion', 'ServerVersion', 'Version', 'BuildNumber'):
                    if key in body and body[key] is not None:
                        version_info['success'] = True
                        version_info['version'] = version_info['version'] or str(body[key])
                        version_info['product_version'] = version_info['product_version'] or str(body.get('ProductVersion', body.get(key)))
                        version_info['raw_excerpt'] = {k: body.get(k) for k in list(body.keys())[:20]}
                        version_info['source'] = 'Configuration'
                        break

    # Response headers sometimes contain version (e.g. Server, X-TM1-*).
    if not version_info.get('version') and result.get('response_headers'):
        headers = result.get('response_headers') or {}
        for h in ('Server', 'X-TM1-Server-Version', 'X-TM1-Version'):
            if h in headers and headers[h]:
                version_info['success'] = True
                version_info['version'] = version_info['version'] or str(headers[h]).strip()
                version_info['source'] = version_info.get('source') or 'response_header'
                break

    return version_info

