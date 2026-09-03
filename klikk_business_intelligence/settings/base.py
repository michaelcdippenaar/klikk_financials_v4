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
    'rest_framework_simplejwt.token_blacklist',  # Truthful v2 refresh-token logout
    'strawberry_django',
    # Local apps
    'apps.user',
    'apps.web_api_v2',  # Authenticated GraphQL boundary for the Vue portal
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
    'apps.personal_expenses',  # Personal-expenses classification + reporting
    'apps.audit',  # Year-end audit registry (audit.checks / check_runs / check_results)
    'apps.receipts',  # Audit -> Receipts review workflow over whatsapp.klikk_slips
    'apps.activity',  # Append-only "who did what" trail over the audit surface
    'apps.pricelist',  # Equipment rate card + effective-dated prices + quote builder
    'apps.kb',  # Books knowledge base — read-only allocation doctrine (kb schema)
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
    # Hard read-only gate for role=auditor accounts (see apps/user/middleware.py).
    # After AuthenticationMiddleware so session users are resolved; only ever
    # SUBTRACTS access from auditor accounts, grants nothing to anyone.
    'apps.user.middleware.AuditorGateMiddleware',
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

# Public base for signed slip-viewer links (apps.audit.slip_view.slip_url / receipts view_url)
SLIP_VIEW_BASE_URL = os.environ.get('SLIP_VIEW_BASE_URL', 'https://console.8-bit.space/backend')

# --- Comment webhook (apps.audit.comment_webhook) ---------------------------
# Empty URL = disabled, which is the default: nothing is POSTed anywhere unless
# an operator opts in. Every attempt is logged to audit.CommentWebhookDelivery
# regardless of outcome, so "who sent what where" is answerable after the fact.
COMMENT_WEBHOOK_URL = (os.environ.get('COMMENT_WEBHOOK_URL') or '').strip()
COMMENT_WEBHOOK_SECRET = (os.environ.get('COMMENT_WEBHOOK_SECRET') or '').strip()
COMMENT_WEBHOOK_TIMEOUT = float(os.environ.get('COMMENT_WEBHOOK_TIMEOUT', '5'))
# Where the console lives, for the deep link in the webhook payload.
CONSOLE_BASE_URL = (os.environ.get('CONSOLE_BASE_URL') or 'https://console.8-bit.space').rstrip('/')
# --- Outbound mail (apps.xero.xero_data.cube_mentions) ----------------------
#
# There were no EMAIL_* settings at all, so every mention fell through to
# Django's own default of localhost:25 and went nowhere. That is not a
# misconfiguration to fix in the env file -- there was nothing there to
# configure, and the failure looked like a mail problem rather than a missing
# one.
#
# Everything reads from the environment with Django's defaults intact, so an
# unset variable leaves behaviour exactly as it is today: nothing starts
# sending because this block merged. DEFAULT_FROM_EMAIL falls back to the
# account actually authenticating, because a From: that does not match the
# authenticated sender is the usual way Gmail SMTP accepts a message and then
# drops it.
EMAIL_BACKEND = (os.environ.get('EMAIL_BACKEND')
                 or 'django.core.mail.backends.smtp.EmailBackend').strip()
EMAIL_HOST = (os.environ.get('EMAIL_HOST') or 'localhost').strip()
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '25'))
EMAIL_HOST_USER = (os.environ.get('EMAIL_HOST_USER') or '').strip()
# Not stripped and never logged: an app password is 16 characters that may be
# stored with the spaces Google displays, and .strip() would hide only the ends
# of a mistake rather than the mistake.
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD') or ''
EMAIL_USE_TLS = (os.environ.get('EMAIL_USE_TLS') or '').strip().lower() in ('1', 'true', 'yes', 'on')
EMAIL_USE_SSL = (os.environ.get('EMAIL_USE_SSL') or '').strip().lower() in ('1', 'true', 'yes', 'on')
# A mention is sent INSIDE the request that saved the comment. Without a
# timeout an unreachable SMTP host holds that request open until the gateway
# gives up, so a mail outage would present as the comments page hanging --
# which is a symptom this console has already paid for once.
EMAIL_TIMEOUT = float(os.environ.get('EMAIL_TIMEOUT', '10'))
DEFAULT_FROM_EMAIL = (os.environ.get('DEFAULT_FROM_EMAIL')
                      or EMAIL_HOST_USER or 'webmaster@localhost').strip()

# Shared service token for machine callers (the klikk-financials MCP server) that have no
# Django user. Consumed by klikk_business_intelligence.permissions.ServiceTokenAuthentication.
# Unset => service-token writes are denied (a warning is logged once).
KLIKK_API_TOKEN = (os.environ.get('KLIKK_API_TOKEN') or '').strip()

# ── Who is behind a shared service credential ───────────────────────────────
#
# The Excel add-in signs in as the Django user `excel-addin` (role
# service_readonly). That name identifies the TOOL, not the person, so stamping
# it on a comment would file every note MC writes under "excel-addin" — true,
# and useless in an author filter. Until 2026-09-03 the pane worked around that
# by asking the operator to TYPE their name into the task pane, which is how
# `ewffew` became a durable author in app.cube_comments.
#
# So the operator behind a service credential is declared HERE, on the server,
# and the client's claim is ignored. The token is the credential and the token
# lives on one person's laptop, so the mapping is a property of the account,
# not of the request — which is exactly what makes it unspoofable: a copied
# token cannot claim to be someone else, it can only be the operator its
# account is mapped to.
#
# Format: "service-username=operator-username[:label]". The operator must name a
# real, active Django user or the mapping is refused at use time (see
# apps.user.identity) -- a typo here must not invent an identity.
#
# The LABEL is what gets stamped, and it is separate from the operator on
# purpose. The operator answers "which real account is accountable for this
# credential"; the label answers "what should the register call them". MC's
# login is mc@tremly.com and his 27 existing pane comments are authored `MC`,
# so stamping the username would split him across two authors in the console's
# author filter -- the exact split he spent an evening removing. Omit the label
# and the operator's username is used.
#
# It is deliberately NOT derived from the user record (first_name / a profile
# name): those are editable from the Django admin, and a register's author
# vocabulary must not change because somebody tidied a profile. This is
# configuration -- explicit, reviewed, and deployed on purpose.
#
# A SECOND person with the pane gets their OWN service user and their own entry
# here, never a share of this one. See excel_addin/README.md § Credentials.
def _parse_operators(raw):
    """{'service-username': (operator_username, stamped_label)}."""
    out = {}
    for pair in (raw or '').split(','):
        service, sep, rest = pair.partition('=')
        if not sep or not service.strip() or not rest.strip():
            continue
        operator, _, label = rest.partition(':')
        operator = operator.strip()
        if not operator:
            continue
        out[service.strip()] = (operator, label.strip() or operator)
    return out


SERVICE_ACCOUNT_OPERATORS = _parse_operators(
    os.environ.get('SERVICE_ACCOUNT_OPERATORS') or 'excel-addin=mc@tremly.com:MC'
)

# Web GraphQL transport limits. Variables and document contents must never be
# written to application logs.
WEB_API_V2_MAX_REQUEST_BYTES = int(os.environ.get('WEB_API_V2_MAX_REQUEST_BYTES', str(256 * 1024)))
WEB_API_V2_MAX_QUERY_DEPTH = int(os.environ.get('WEB_API_V2_MAX_QUERY_DEPTH', '8'))
WEB_API_V2_MAX_QUERY_TOKENS = int(os.environ.get('WEB_API_V2_MAX_QUERY_TOKENS', '1000'))
# The real browser documents measure 17-51 field selections; the Xero pipeline
# read is the largest and grew past the original ceiling of 50 when source
# evidence was added. 100 keeps a genuine ceiling with roughly 2x headroom.
# Query depth (8) and token count remain the other two guards.
WEB_API_V2_MAX_FIELD_SELECTIONS = int(os.environ.get('WEB_API_V2_MAX_FIELD_SELECTIONS', '100'))
WEB_API_V2_AUTH_MAX_REQUEST_BYTES = int(
    os.environ.get('WEB_API_V2_AUTH_MAX_REQUEST_BYTES', str(16 * 1024))
)
WEB_API_V2_INGEST_MAX_REQUEST_BYTES = int(
    os.environ.get('WEB_API_V2_INGEST_MAX_REQUEST_BYTES', str(16 * 1024))
)
WEB_API_V2_INGEST_MAX_XERO_CALLS = int(
    os.environ.get('WEB_API_V2_INGEST_MAX_XERO_CALLS', '50')
)
# Declares that this deployment actually runs `manage.py run_ingest_worker`.
# Off by default: enabling it lets Standard sync consume real Xero API budget,
# and two production blowouts in August 2026 came from unbounded Xero calls.
WEB_API_V2_INGEST_WORKER_ENABLED = os.environ.get(
    'WEB_API_V2_INGEST_WORKER_ENABLED', 'false',
).lower() in {'1', 'true', 'yes'}

WEB_API_V2_INGEST_RUN_LEASE_SECONDS = int(
    os.environ.get('WEB_API_V2_INGEST_RUN_LEASE_SECONDS', '1800')
)

# REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        # Must run BEFORE JWTAuthentication: that class raises InvalidToken (401) on an opaque
        # Bearer token, so a permission class alone could never accept the service token.
        'klikk_business_intelligence.permissions.ServiceTokenAuthentication',  # Shared service token (MCP)
        'rest_framework_simplejwt.authentication.JWTAuthentication',  # JWT authentication
        'rest_framework.authentication.TokenAuthentication',  # Keep for backward compatibility
        'rest_framework.authentication.SessionAuthentication',
    ],
    # SECURITY (2026-08-20): flipped from AllowAny after SECURITY-NOTE.md documented
    # ~90 anonymously reachable endpoints (full GL, Investec bank data, personal
    # expenses) on the public internet. Every DRF view is now authenticated by
    # default. The ONLY views that may declare AllowAny are the credential
    # bootstrap paths (login/refresh/token, the nginx auth_request check) and the
    # Xero OAuth callback — each carries a comment saying why. Do NOT add more.
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # Anonymous callers share one modest bucket (they can only reach login/token
    # endpoints and the Xero callback now). Authenticated traffic is unthrottled.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/min',
        'v2_auth_login': '10/min',
        'v2_auth_refresh': '30/min',
        'v2_auth_verify': '60/min',
        'v2_auth_logout': '30/min',
        'v2_ingest_reads': '120/min',
        'v2_ingest_commands': '10/hour',
    },
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

# Xero per-tenant daily API call cap. Klikk's tenant is capped at 1,000/day
# (fixed window, resets ~14:26 UTC), not the 5,000 default in Xero's docs.
XERO_DAILY_CALL_CAP = int(os.environ.get('XERO_DAILY_CALL_CAP', '1000'))

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

# Optional: map Investec profileId (or accountNumber) -> owner label for the
# personal-expenses grouping (e.g. MC vs Wife). Env format:
#   INVESTEC_OWNER_MAP="profileId1=MC,profileId2=Wife"   (or accountNumber=Owner)
INVESTEC_OWNER_MAP = {
    kv.split('=', 1)[0].strip(): kv.split('=', 1)[1].strip()
    for kv in (os.environ.get('INVESTEC_OWNER_MAP') or '').split(',')
    if '=' in kv
}

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
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:9000')
XERO_REDIRECT_URI = os.environ.get('XERO_REDIRECT_URI')
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
# Prewarming opens a connection to TM1 at startup. It is additionally gated to
# serving processes only; see apps/ai_agent/apps.py.
AI_AGENT_TM1_PREWARM = os.environ.get('AI_AGENT_TM1_PREWARM', 'true').lower() not in {'0', 'false', 'no'}

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
