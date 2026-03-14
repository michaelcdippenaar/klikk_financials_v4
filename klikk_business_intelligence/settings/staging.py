"""
Staging environment settings for klikk_business_intelligence project.

These settings are used for the staging server.
"""

from .base import *
import os

# SECURITY WARNING: keep the secret key used in production secret!
# In staging, use environment variable or a secure key
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-staging-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

# Update with your staging server domain
# Include: IP address, hostname, domain name (if applicable), localhost, and 127.0.0.1
# Include host:port for direct access (e.g. http://192.168.1.235:8001/admin/)
_base = os.environ.get(
    'ALLOWED_HOSTS',
    'localhost,127.0.0.1,192.168.1.235,102.135.240.222,www.klikk.co.za,klikk.co.za,paw.klikk.co.za'
).split(',')
_extra = ['192.168.1.235', '127.0.0.1:8001', '192.168.1.235:8001']
ALLOWED_HOSTS = [h.strip() for h in _base if h.strip()] + [h for h in _extra if h not in _base]

# CORS - allow the staging server to serve the frontend (HTTP and HTTPS)
# :8080 = portal container direct; :9000 = legacy
CORS_ALLOWED_ORIGINS = [
    'https://www.klikk.co.za',
    'https://klikk.co.za',
    'https://paw.klikk.co.za',
    'http://102.135.240.222',
    'https://102.135.240.222',
    'http://102.135.240.222:9000',
    'https://102.135.240.222:9000',
    'http://102.135.240.222:8080',
    'https://102.135.240.222:8080',
    'http://192.168.1.235',
    'https://192.168.1.235',
    'http://192.168.1.235:9000',
    'https://192.168.1.235:9000',
    'http://192.168.1.235:8080',
    'https://192.168.1.235:8080',
    'http://localhost:9000',
    'https://localhost:9000',
    'http://localhost:8080',
    'https://localhost:8080',
    'http://127.0.0.1:9000',
    'https://127.0.0.1:9000',
    'http://127.0.0.1:8080',
    'https://127.0.0.1:8080',
]
CORS_ALLOW_CREDENTIALS = True

# Database – all values read from environment variables so Docker/bare-metal both work.
# Bare-metal default: HOST=127.0.0.1 (postgres on same host)
# Docker default:     HOST=db        (set in docker-compose.yml via environment: DB_HOST=db)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'klikk_financials_v4'),
        'USER': os.environ.get('DB_USER', 'klikk_user'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'Number55dip'),
        'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
        'PORT': os.environ.get('DB_PORT', '5432'),
        'CONN_MAX_AGE': 0,
    }
}

# Google Cloud credentials path (for BigQuery exports)
# Set via environment variable: GOOGLE_APPLICATION_CREDENTIALS
# Example: export GOOGLE_APPLICATION_CREDENTIALS=/home/mc/apps/klikk_financials_v3/credentials/klick-financials01-81b1aeed281d.json
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')

# GitHub webhook secret for deployment
# Generate a secure random string: python -c "import secrets; print(secrets.token_urlsafe(32))"
GITHUB_WEBHOOK_SECRET = os.environ.get('GITHUB_WEBHOOK_SECRET', '')

# Update JWT signing key
SIMPLE_JWT['SIGNING_KEY'] = SECRET_KEY

# Static files configuration for staging (served by WhiteNoise)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

# Media files (if you have user uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Security settings for staging (HTTPS via nginx)
SECURE_SSL_REDIRECT = False  # nginx handles HTTP→HTTPS redirect
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# Allow cookies over HTTP so admin works at http://192.168.1.235:8001/admin/
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
CSRF_TRUSTED_ORIGINS = [
    'https://www.klikk.co.za',
    'https://klikk.co.za',
    'https://paw.klikk.co.za',
    'http://192.168.1.235',
    'https://192.168.1.235',
    'http://192.168.1.235:8001',
    'https://192.168.1.235:8001',
    'http://192.168.1.235:8080',
    'https://192.168.1.235:8080',

    'http://192.168.1.235:9000',
    'https://192.168.1.235:9000',
    'http://102.135.240.222',
    'https://102.135.240.222',
    'http://102.135.240.222:8080',
    'https://102.135.240.222:8080',
    'http://localhost:8080',
    'https://localhost:8080',
    'http://127.0.0.1:8001',
    'http://127.0.0.1:8080',
    'https://127.0.0.1:8080',
]

# Xero Scheduler Configuration
XERO_SCHEDULER_ENABLED = True  # Enabled for staging
