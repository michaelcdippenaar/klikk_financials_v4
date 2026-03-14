"""
Base settings for klikk_business_intelligence project.

These settings are shared across all environments (development, staging, production).
Environment-specific settings should be defined in their respective files.
"""

import os
from pathlib import Path
from datetime import timedelta

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

def _load_env_file(path: Path) -> None:
    """
    Lightweight .env loader (no dependency).
    Uses os.environ.setdefault so real environment vars win.
    """
    try:
        if not path.exists() or not path.is_file():
            return
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key:
                os.environ.setdefault(key, value)
    except Exception:
        # Never fail app startup due to env parsing.
        return


# Load local env files early so settings modules can read os.environ.
_load_env_file(BASE_DIR / ".env")
_load_env_file(BASE_DIR / ".env.local")


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'corsheaders',
    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt',  # JWT authentication
    # Local apps
    'apps.user',
    'apps.deployment',  # GitHub webhook for automatic deployment
    # Xero apps
    'apps.xero.xero_auth',
    'apps.xero.xero_core',
    'apps.xero.xero_cube',
    'apps.xero.xero_data',
    'apps.xero.xero_integration',
    'apps.xero.xero_metadata',
    'apps.xero.xero_sync',
    'apps.xero.xero_validation',
    'apps.investec',
    'apps.financial_investments',
    'apps.planning_analytics',
    'apps.ai_agent',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'klikk_business_intelligence.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'klikk_business_intelligence.wsgi.application'


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
# When the app runs on the document server (e.g. 192.168.1.235), set MEDIA_ROOT to a path on that host.
# Example: MEDIA_ROOT=/var/data/klikk_financials_v4/media
MEDIA_ROOT = os.environ.get('MEDIA_ROOT') or str(BASE_DIR / 'media')
if not os.path.isabs(MEDIA_ROOT):
    MEDIA_ROOT = str(BASE_DIR / MEDIA_ROOT)

# Optional: dedicated root for Xero-imported documents (e.g. on 192.168.1.235). If set, XeroDocument files are stored here.
# Example: XERO_DOCUMENTS_ROOT=/var/data/klikk_financials_v4/xero_documents
XERO_DOCUMENTS_ROOT = os.environ.get('XERO_DOCUMENTS_ROOT') or None

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom User Model
AUTH_USER_MODEL = 'user.User'

# REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',  # JWT authentication
        'rest_framework.authentication.TokenAuthentication',  # Keep for backward compatibility
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',  # Views can override with IsAuthenticated if needed
    ],
}

# JWT Configuration
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),  # Access token expires in 1 hour
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),  # Refresh token expires in 7 days
    'ROTATE_REFRESH_TOKENS': True,  # Generate new refresh token on refresh
    'BLACKLIST_AFTER_ROTATION': True,  # Blacklist old refresh tokens
    'UPDATE_LAST_LOGIN': True,  # Update user's last_login field
    
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': None,  # Will be set in environment-specific files
    'VERIFYING_KEY': None,
    'AUDIENCE': None,
    'ISSUER': None,
    
    'AUTH_HEADER_TYPES': ('Bearer',),  # Authorization: Bearer <token>
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
    
    'JTI_CLAIM': 'jti',  # JWT ID claim
    
    'SLIDING_TOKEN_REFRESH_EXP_CLAIM': 'refresh_exp',
    'SLIDING_TOKEN_LIFETIME': timedelta(minutes=60),
    'SLIDING_TOKEN_REFRESH_LIFETIME': timedelta(days=1),
}

# Xero Scheduler Configuration
XERO_SCHEDULER_ENABLED = False  # Set to False to disable scheduler

# Investec Private Banking API (SA PB Account Information)
# Credentials: set INVESTEC_CLIENT_ID, INVESTEC_CLIENT_SECRET, INVESTEC_API_KEY (x-api-key) in env or .env
# Optional: INVESTEC_BASE_URL (default production; use https://openapisandbox.investec.com for sandbox)
# Multiple profiles supported: add _2, _3 etc. suffix for additional credential sets (share same API key).
INVESTEC_BASE_URL = os.environ.get('INVESTEC_BASE_URL') or os.environ.get('investec_base_url') or 'https://openapi.investec.com'
INVESTEC_CLIENT_ID = os.environ.get('INVESTEC_CLIENT_ID') or os.environ.get('investec_client_id') or ''
INVESTEC_CLIENT_SECRET = os.environ.get('INVESTEC_CLIENT_SECRET') or os.environ.get('investec_client_secret') or ''
INVESTEC_API_KEY = os.environ.get('INVESTEC_API_KEY') or os.environ.get('investec_key') or ''

def _build_investec_profiles():
    """Collect all Investec credential profiles from env. Returns list of dicts with client_id, client_secret, api_key."""
    profiles = []
    base_id = INVESTEC_CLIENT_ID
    base_secret = INVESTEC_CLIENT_SECRET
    base_key = INVESTEC_API_KEY
    if base_id and base_secret and base_key:
        profiles.append({'client_id': base_id, 'client_secret': base_secret, 'api_key': base_key})
    i = 2
    while True:
        cid = os.environ.get(f'INVESTEC_CLIENT_ID_{i}') or os.environ.get(f'investec_client_id_{i}') or ''
        csec = os.environ.get(f'INVESTEC_CLIENT_SECRET_{i}') or os.environ.get(f'investec_client_secret_{i}') or ''
        ckey = os.environ.get(f'INVESTEC_API_KEY_{i}') or os.environ.get(f'investec_key_{i}') or ''
        if not cid and not csec:
            break
        profiles.append({
            'client_id': cid,
            'client_secret': csec,
            'api_key': ckey or base_key,
        })
        i += 1
    return profiles

INVESTEC_PROFILES = _build_investec_profiles()

# TM1 / IBM Planning Analytics — default server (used when no TM1ServerConfig in DB).
# Trail balance: cube Trail_Balance, source gl_src_trail_balance; TI import process cub.gl_src_trial_balance.import
TM1_CONFIG = {
    'address': os.environ.get('TM1_ADDRESS', '192.168.1.194'),
    'port': int(os.environ.get('TM1_PORT', '44414')),
    'user': os.environ.get('TM1_USER', 'mc'),
    'password': os.environ.get('TM1_PASSWORD', 'pass'),
    'ssl': os.environ.get('TM1_SSL', 'false').lower() in ('true', '1', 'yes'),
}
_scheme = 'https' if TM1_CONFIG['ssl'] else 'http'
TM1_BASE_URL = os.environ.get('TM1_BASE_URL') or f"{_scheme}://{TM1_CONFIG['address']}:{TM1_CONFIG['port']}/api/v1"
TM1_USER = TM1_CONFIG['user']
TM1_PASSWORD = TM1_CONFIG['password']
# EOD Historical Data API (optional) — JSE stock data, fundamentals, bulk exchange tickers
# Get key at https://eodhd.com/
EOD_API_KEY = os.environ.get('EOD_API_KEY') or os.environ.get('eod_api_key') or ''

TM1_VERIFY_SSL = os.environ.get('TM1_VERIFY_SSL', 'false').lower() in ('true', '1', 'yes')  # Set True to verify HTTPS certs
TM1_REQUEST_TIMEOUT = int(os.environ.get('TM1_REQUEST_TIMEOUT', '300'))

# ---------------------------------------------------------------------------
# AI Agent MCP Skills Engine
# These settings mirror the FastAPI config.py Settings class so that
# the migrated skill modules can use the same attribute names.
# ---------------------------------------------------------------------------

# AI Provider toggle: "anthropic" or "openai"
AI_AGENT_PROVIDER = os.environ.get('AI_AGENT_PROVIDER') or os.environ.get('AI_PROVIDER', 'openai')

# Anthropic (Claude)
AI_AGENT_ANTHROPIC_API_KEY = os.environ.get('AI_AGENT_ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY', '')
AI_AGENT_ANTHROPIC_MODEL = os.environ.get('AI_AGENT_ANTHROPIC_MODEL') or os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6')

# OpenAI
AI_AGENT_OPENAI_API_KEY = os.environ.get('AI_AGENT_OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY', '')
AI_AGENT_OPENAI_MODEL = os.environ.get('AI_AGENT_OPENAI_MODEL') or os.environ.get('OPENAI_MODEL', 'gpt-4o')

# Shared AI settings
AI_AGENT_MAX_TOKENS = int(os.environ.get('AI_AGENT_MAX_TOKENS', '2048'))
AI_AGENT_MAX_TOOL_ROUNDS = int(os.environ.get('AI_AGENT_MAX_TOOL_ROUNDS', '8'))

# Local sentence-transformers embeddings (all-MiniLM-L6-v2, 384-dim)
AI_AGENT_VOYAGE_API_KEY = os.environ.get('VOYAGE_API_KEY', '')
AI_AGENT_VOYAGE_MODEL = os.environ.get('VOYAGE_MODEL', 'voyage-3-lite')
AI_AGENT_EMBEDDING_DIM = int(os.environ.get('EMBEDDING_DIM', '384'))

# RAG settings (vectors stored in default klikk_financials_v4 database)
AI_AGENT_RAG_TOP_K = int(os.environ.get('RAG_TOP_K', '5'))
AI_AGENT_RAG_MIN_SCORE = float(os.environ.get('RAG_MIN_SCORE', '0.20'))

# PAW (Planning Analytics Workspace)
AI_AGENT_PAW_HOST = os.environ.get('PAW_HOST', '192.168.1.194')
AI_AGENT_PAW_PORT = int(os.environ.get('PAW_PORT', '8080'))
AI_AGENT_PAW_ENABLED = os.environ.get('PAW_ENABLED', 'true').lower() in ('true', '1', 'yes')
AI_AGENT_PAW_SERVER_NAME = os.environ.get('PAW_SERVER_NAME', '')

# Web Search
AI_AGENT_WEB_SEARCH_ENABLED = os.environ.get('WEB_SEARCH_ENABLED', 'true').lower() in ('true', '1', 'yes')
AI_AGENT_WEB_SEARCH_PROVIDER = os.environ.get('WEB_SEARCH_PROVIDER', 'duckduckgo')
AI_AGENT_WEB_SEARCH_API_KEY = os.environ.get('WEB_SEARCH_API_KEY', '')
AI_AGENT_WEB_SEARCH_MAX_RESULTS = int(os.environ.get('WEB_SEARCH_MAX_RESULTS', '5'))

# Google Drive (optional)
AI_AGENT_GOOGLE_DRIVE_ENABLED = os.environ.get('GOOGLE_DRIVE_ENABLED', 'false').lower() in ('true', '1', 'yes')
AI_AGENT_GOOGLE_DRIVE_CREDENTIALS_PATH = os.environ.get('GOOGLE_DRIVE_CREDENTIALS_PATH', '')
AI_AGENT_GOOGLE_DRIVE_FOLDER_IDS = os.environ.get('GOOGLE_DRIVE_FOLDER_IDS', '')

# Klikk Financials API (vectorized RAG, corpora search)
AI_AGENT_FINANCIALS_API_TOKEN = os.environ.get('FINANCIALS_API_TOKEN', '')

# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'readable': {
            'format': '%(asctime)s %(levelname)-5s [%(name)s] %(message)s',
            'datefmt': '%H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'readable',
        },
    },
    'loggers': {
        'ai_agent': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'mcp_tm1': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}