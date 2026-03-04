"""
Development environment settings for klikk_business_intelligence project.

These settings are used for local development.
"""
import os

from .base import *

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-ri#xovh+9i8oys0j=w88o!a&jkiwf@9j_3i69^*+af(-k6d%rp'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0']

# CORS - allow frontend dev server (portal may run on 9000, 8080, 5173, etc.)
CORS_ALLOWED_ORIGINS = [
    'http://localhost:9000',
    'http://127.0.0.1:9000',
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
]
CORS_ALLOW_CREDENTIALS = True

# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'klikk_financials_v4',
        'USER': 'klikk_user',
        'PASSWORD': 'Number55dip',
        'HOST': '192.168.1.235',
        'PORT': '5432',
        'CONN_MAX_AGE': 600,  # Reuse connections for 10 minutes (connection pooling)
        'OPTIONS': {
            'connect_timeout': 10,
        }
    },
    # v3 DB for copying credentials/settings (development only)
    'v3': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'klikk_bi_v3',
        'USER': 'mc',
        'PASSWORD': 'Number55dip',
        'HOST': '127.0.0.1',
        'PORT': '5432',
        'OPTIONS': {'connect_timeout': 10},
    }
}

# Update JWT signing key
SIMPLE_JWT['SIGNING_KEY'] = SECRET_KEY

# Xero Scheduler Configuration
XERO_SCHEDULER_ENABLED = False  # Disabled for development

# Google Cloud credentials (BigQuery export). Uses v3 credentials if v4 and v3 are sibling directories.
_v4_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_v3_creds = os.path.join(os.path.dirname(_v4_root), 'klikk_financials_v3', 'credentials', 'klick-financials01-81b1aeed281d.json')
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or (_v3_creds if os.path.exists(_v3_creds) else None)

# AI Agent Configuration
AI_AGENT_OPENAI_API_KEY = os.environ.get("AI_AGENT_OPENAI_API_KEY", "")
# OpenAI chat model: gpt-4o-mini, gpt-4o, gpt-5.2, gpt-5.2-instant, gpt-5.2-thinking, gpt-5.2-pro, gpt-5-mini, etc.
AI_AGENT_MODEL = os.environ.get("AI_AGENT_MODEL", "gpt-5.2")
# Web search (Serper): get a key at https://serper.dev to enable "current price", "look up", stock quotes, etc.
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

# Keep Gemini disabled in development so OpenAI is used explicitly.
AI_AGENT_GEMINI_API_KEY = ""
AI_AGENT_GEMINI_MODEL = "gemini-2.5-flash"

# Embeddings (vectorisation)
# Set in your shell, e.g.:
# export AI_AGENT_EMBEDDING_MODEL="text-embedding-3-small"
AI_AGENT_EMBEDDING_MODEL = os.environ.get("AI_AGENT_EMBEDDING_MODEL", "text-embedding-3-small")

# Development-only: disable ai_agent auth + TM1 write policy guardrails for easier testing.
AI_AGENT_DISABLE_SECURITY = True

