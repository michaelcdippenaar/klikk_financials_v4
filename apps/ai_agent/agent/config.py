"""
Settings adapter for the AI agent skills engine.

Maps django.conf.settings.AI_AGENT_* to the flat attribute names
that all skill modules expect (e.g. settings.anthropic_api_key).
Also checks the Credential model (DB-first) with a 60s in-memory cache.

Usage in skill files:
    from apps.ai_agent.agent.config import settings, get_credential
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from django.conf import settings as django_settings
from django.db import connection

log = logging.getLogger('ai_agent')

# ---------------------------------------------------------------------------
# Credential cache (DB-first, fallback to Django settings)
# ---------------------------------------------------------------------------
_cred_cache: dict[str, tuple[str, float]] = {}
_cred_lock = threading.Lock()
_CRED_TTL = 60  # seconds


def get_credential(key: str) -> str:
    """Get a credential — checks Credential model first (cached 60s), falls back to settings."""
    # Check cache
    with _cred_lock:
        entry = _cred_cache.get(key)
        if entry and (time.monotonic() - entry[1]) < _CRED_TTL:
            return entry[0]

    # Try DB
    try:
        from apps.ai_agent.models import Credential
        cred = Credential.objects.filter(key=key).values_list('value', flat=True).first()
        if cred:
            with _cred_lock:
                _cred_cache[key] = (cred, time.monotonic())
            return cred
    except Exception:
        pass

    # Fallback to Django settings (use django_settings directly to avoid recursion)
    django_key = f'AI_AGENT_{key.upper()}'
    fallback = getattr(django_settings, django_key, '')
    with _cred_lock:
        _cred_cache[key] = (fallback, time.monotonic())
    return fallback


def invalidate_credential_cache(key: str = ''):
    """Clear credential cache entry (or all if no key given)."""
    with _cred_lock:
        if key:
            _cred_cache.pop(key, None)
        else:
            _cred_cache.clear()


# ---------------------------------------------------------------------------
# Settings proxy — exposes same attribute names as FastAPI config.Settings
# ---------------------------------------------------------------------------

class _SettingsProxy:
    """Proxy that maps attribute access to django.conf.settings.AI_AGENT_*
    and also provides credential-aware lookups for API keys.
    """

    # Map: FastAPI attr name → Django settings attr name
    _ATTR_MAP = {
        # AI Provider
        'ai_provider':          'AI_AGENT_PROVIDER',
        'anthropic_api_key':    'AI_AGENT_ANTHROPIC_API_KEY',
        'anthropic_model':      'AI_AGENT_ANTHROPIC_MODEL',
        'openai_api_key':       'AI_AGENT_OPENAI_API_KEY',
        'openai_model':         'AI_AGENT_OPENAI_MODEL',
        'max_tokens':           'AI_AGENT_MAX_TOKENS',
        'max_tool_rounds':      'AI_AGENT_MAX_TOOL_ROUNDS',

        # Embeddings (local sentence-transformers, no API key needed)
        'embedding_dim':        'AI_AGENT_EMBEDDING_DIM',

        # TM1
        'tm1_host':             'TM1_CONFIG',  # special — extracted from dict
        'tm1_port':             'TM1_CONFIG',
        'tm1_user':             'TM1_CONFIG',
        'tm1_password':         'TM1_CONFIG',
        'tm1_ssl':              'TM1_CONFIG',

        # PostgreSQL klikk_financials (uses Django default DB)
        'pg_financials_host':   '_DB_DEFAULT',
        'pg_financials_port':   '_DB_DEFAULT',
        'pg_financials_db':     '_DB_DEFAULT',
        'pg_financials_user':   '_DB_DEFAULT',
        'pg_financials_password': '_DB_DEFAULT',

        # PostgreSQL bi_etl — same as default now (bi_etl retired)
        'pg_bi_host':           '_DB_DEFAULT',
        'pg_bi_port':           '_DB_DEFAULT',
        'pg_bi_db':             '_DB_DEFAULT',
        'pg_bi_user':           '_DB_DEFAULT',
        'pg_bi_password':       '_DB_DEFAULT',

        # RAG
        'rag_schema':           'public',  # no longer using agent_rag schema
        'rag_top_k':            'AI_AGENT_RAG_TOP_K',
        'rag_min_score':        'AI_AGENT_RAG_MIN_SCORE',

        # PAW
        'paw_host':             'AI_AGENT_PAW_HOST',
        'paw_port':             'AI_AGENT_PAW_PORT',
        'paw_enabled':          'AI_AGENT_PAW_ENABLED',
        'paw_server_name':      'AI_AGENT_PAW_SERVER_NAME',

        # Web Search
        'web_search_enabled':   'AI_AGENT_WEB_SEARCH_ENABLED',
        'web_search_provider':  'AI_AGENT_WEB_SEARCH_PROVIDER',
        'web_search_api_key':   'AI_AGENT_WEB_SEARCH_API_KEY',
        'web_search_max_results': 'AI_AGENT_WEB_SEARCH_MAX_RESULTS',

        # Google Drive
        'google_drive_enabled': 'AI_AGENT_GOOGLE_DRIVE_ENABLED',
        'google_drive_credentials_path': 'AI_AGENT_GOOGLE_DRIVE_CREDENTIALS_PATH',
        'google_drive_folder_ids': 'AI_AGENT_GOOGLE_DRIVE_FOLDER_IDS',

        # Klikk Financials API
        'financials_api_token': 'AI_AGENT_FINANCIALS_API_TOKEN',

        # Auth
        'auth_required':        True,  # always required in Django
    }

    # Keys that should be looked up via get_credential() (DB-first)
    _CREDENTIAL_KEYS = {
        'anthropic_api_key', 'openai_api_key',
        'web_search_api_key', 'financials_api_token',
    }

    def __getattr__(self, name: str) -> Any:
        # Credential-backed keys: check DB first, then Django settings
        if name in self._CREDENTIAL_KEYS:
            val = get_credential(name)
            if val:
                return val
            # If DB returned nothing, fall through to normal resolution

        mapping = self._ATTR_MAP.get(name)

        # TM1 config — extract from dict
        if mapping == 'TM1_CONFIG':
            tm1_cfg = getattr(django_settings, 'TM1_CONFIG', {})
            tm1_key = name.replace('tm1_', '')  # tm1_host → host
            if tm1_key == 'host':
                tm1_key = 'address'
            return tm1_cfg.get(tm1_key, '' if tm1_key != 'port' else 44414)

        # Default DB connection params
        if mapping == '_DB_DEFAULT':
            db = django_settings.DATABASES.get('default', {})
            field_map = {
                'pg_financials_host': 'HOST',
                'pg_financials_port': 'PORT',
                'pg_financials_db': 'NAME',
                'pg_financials_user': 'USER',
                'pg_financials_password': 'PASSWORD',
                'pg_bi_host': 'HOST',
                'pg_bi_port': 'PORT',
                'pg_bi_db': 'NAME',
                'pg_bi_user': 'USER',
                'pg_bi_password': 'PASSWORD',
            }
            db_key = field_map.get(name, '')
            val = db.get(db_key, '')
            if name.endswith('_port'):
                return int(val) if val else 5432
            return val

        # Static value
        if mapping is not None and not isinstance(mapping, str):
            return mapping

        # RAG schema — now just 'public'
        if name == 'rag_schema':
            return 'public'

        # Normal Django settings lookup
        if mapping and isinstance(mapping, str):
            return getattr(django_settings, mapping, '')

        # Last resort: check Django settings directly
        django_name = f'AI_AGENT_{name.upper()}'
        if hasattr(django_settings, django_name):
            return getattr(django_settings, django_name)

        raise AttributeError(f"Settings has no attribute '{name}'")


settings = _SettingsProxy()

# Convenience dict for TM1py
TM1_CONFIG = {
    'address': settings.tm1_host,
    'port': settings.tm1_port,
    'user': settings.tm1_user,
    'password': settings.tm1_password,
    'ssl': settings.tm1_ssl,
}
