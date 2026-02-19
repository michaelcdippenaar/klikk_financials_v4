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
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1,192.168.1.235,102.135.240.222').split(',')

# CORS - allow the staging server to serve the frontend
CORS_ALLOWED_ORIGINS = [
    'http://102.135.240.222',
    'http://102.135.240.222:9000',
    'http://localhost:9000',
    'http://127.0.0.1:9000',
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

# Security settings for staging
SECURE_SSL_REDIRECT = False  # Set to True if using HTTPS
SESSION_COOKIE_SECURE = False  # Set to True if using HTTPS
CSRF_COOKIE_SECURE = False  # Set to True if using HTTPS

# Xero Scheduler Configuration
XERO_SCHEDULER_ENABLED = True  # Enabled for staging
