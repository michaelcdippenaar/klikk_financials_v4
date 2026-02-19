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

# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'klikk_financials_v4',
        'USER': 'klikk_user',
        'PASSWORD': 'Number55dip',
        'HOST': '127.0.0.1',
        'PORT': '5432',
        # For gunicorn with multiple workers, use 0 to disable connection pooling
        # Each worker will manage its own connections
        'CONN_MAX_AGE': 0,  # Disable persistent connections for gunicorn workers
        # 'OPTIONS': {
        #     'connect_timeout': 10,
        #     # Additional options for better connection handling
        #     'keepalives': 1,
        #     'keepalives_idle': 30,
        #     'keepalives_interval': 10,
        #     'keepalives_count': 5,
        # }
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

# Static files configuration for staging
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (if you have user uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Use WhiteNoise to serve static files when DEBUG=False
# Install with: pip install whitenoise
# Add 'whitenoise.middleware.WhiteNoiseMiddleware' to MIDDLEWARE (already in base.py)
# WhiteNoise should be added after SecurityMiddleware and before other middleware
# For now, we'll serve static files via URL patterns (see urls.py)

# Security settings for staging
SECURE_SSL_REDIRECT = False  # Set to True if using HTTPS
SESSION_COOKIE_SECURE = False  # Set to True if using HTTPS
CSRF_COOKIE_SECURE = False  # Set to True if using HTTPS

# Xero Scheduler Configuration
XERO_SCHEDULER_ENABLED = True  # Enabled for staging
