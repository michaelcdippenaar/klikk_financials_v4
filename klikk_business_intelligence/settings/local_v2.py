"""Locked-down settings for the isolated local V2 browser environment.

This module deliberately does not inherit either development or staging
settings.  It blocks integration credentials before importing base settings,
uses a dedicated database name, and exposes only the local V2 URLconf.
"""

import os


_BLANK_ENVIRONMENT_KEYS = (
    "AI_AGENT_ANTHROPIC_API_KEY",
    "AI_AGENT_FINANCIALS_API_TOKEN",
    "AI_AGENT_GEMINI_API_KEY",
    "AI_AGENT_GOOGLE_DRIVE_CREDENTIALS_PATH",
    "AI_AGENT_GOOGLE_DRIVE_FOLDER_IDS",
    "AI_AGENT_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "EOD_API_KEY",
    "FINANCIALS_API_TOKEN",
    "GEMINI_API_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "INVESTEC_API_KEY",
    "INVESTEC_CLIENT_ID",
    "INVESTEC_CLIENT_SECRET",
    "INVESTEC_OWNER_MAP",
    "KLIKK_API_TOKEN",
    "OPENAI_API_KEY",
    "SERPER_API_KEY",
    "VOYAGE_API_KEY",
    "WEB_SEARCH_API_KEY",
    "XERO_REDIRECT_URI",
)
for _key in _BLANK_ENVIRONMENT_KEYS:
    os.environ[_key] = ""
for _index in range(2, 11):
    os.environ[f"INVESTEC_API_KEY_{_index}"] = ""
    os.environ[f"INVESTEC_CLIENT_ID_{_index}"] = ""
    os.environ[f"INVESTEC_CLIENT_SECRET_{_index}"] = ""

# Safe loopback sentinels prevent base.py from falling back to live endpoints.
os.environ["INVESTEC_BASE_URL"] = "http://127.0.0.1:1"
os.environ["PAW_ENABLED"] = "false"
os.environ["PAW_HOST"] = "127.0.0.1"
os.environ["PAW_PORT"] = "1"
os.environ["TM1_ADDRESS"] = "127.0.0.1"
os.environ["TM1_BASE_URL"] = "http://127.0.0.1:1/api/v1"
os.environ["TM1_PASSWORD"] = ""
os.environ["TM1_PORT"] = "1"
os.environ["TM1_USER"] = ""
os.environ["WEB_SEARCH_ENABLED"] = "false"

from .base import *  # noqa: E402,F401,F403


LOCAL_V2_SAFE_MODE = True
DEBUG = False

SECRET_KEY = os.environ.get("LOCAL_V2_DJANGO_SECRET_KEY", "")
if len(SECRET_KEY) < 32:
    raise ValueError("LOCAL_V2_DJANGO_SECRET_KEY must contain at least 32 characters.")

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "testserver",
    "klikk-v2-local-backend",
]
ROOT_URLCONF = "klikk_business_intelligence.local_v2_urls"

# Only apps required by the V2 read/auth boundary and its Xero read models are
# installed.  Integration, scheduler, webhook, AI, TM1, Investec and unrelated
# application startup hooks are absent.
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "corsheaders",
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "strawberry_django",
    "apps.user",
    "apps.xero.xero_core",
    "apps.xero.xero_auth",
    "apps.xero.xero_metadata",
    "apps.xero.xero_data",
    "apps.xero.xero_cube",
    "apps.xero.xero_sync",
    "apps.xero.xero_validation",
    "apps.web_api_v2",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

_database_name = os.environ.get("DB_NAME", "")
if not _database_name.startswith("klikk_v2_local"):
    raise ValueError("Local V2 DB_NAME must start with 'klikk_v2_local'.")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _database_name,
        "USER": os.environ.get("DB_USER", "klikk_v2_local"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "klikk-v2-local-postgres"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 3},
    },
}

SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY
SIMPLE_JWT["UPDATE_LAST_LOGIN"] = False

CORS_ALLOWED_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
]
CORS_ALLOW_CREDENTIALS = False
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"

XERO_SCHEDULER_ENABLED = False
KLIKK_API_TOKEN = ""
XERO_REDIRECT_URI = None
XERO_DOCUMENTS_ROOT = None
INVESTEC_BASE_URL = "http://127.0.0.1:1"
INVESTEC_CLIENT_ID = ""
INVESTEC_CLIENT_SECRET = ""
INVESTEC_API_KEY = ""
INVESTEC_PROFILES = []
TM1_CONFIG = {
    "address": "127.0.0.1",
    "port": 1,
    "user": "",
    "password": "",
    "ssl": False,
}
TM1_BASE_URL = "http://127.0.0.1:1/api/v1"
AI_AGENT_PAW_ENABLED = False
AI_AGENT_WEB_SEARCH_ENABLED = False
AI_AGENT_GOOGLE_DRIVE_ENABLED = False
AI_AGENT_ANTHROPIC_API_KEY = ""
AI_AGENT_OPENAI_API_KEY = ""
AI_AGENT_VOYAGE_API_KEY = ""
AI_AGENT_FINANCIALS_API_TOKEN = ""
GOOGLE_APPLICATION_CREDENTIALS = None
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

MEDIA_ROOT = os.environ.get("LOCAL_V2_MEDIA_ROOT", "/tmp/klikk-v2-local-media")
STATIC_ROOT = os.environ.get("LOCAL_V2_STATIC_ROOT", "/tmp/klikk-v2-local-static")

