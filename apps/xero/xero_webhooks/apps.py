from django.apps import AppConfig


class XeroWebhooksConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.xero.xero_webhooks'
    label = 'xero_webhooks'
    verbose_name = 'Xero Webhooks'
