from django.apps import AppConfig


class XeroMetadataConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.xero.xero_metadata'
    verbose_name = 'Xero Metadata'

    def ready(self):
        import apps.xero.xero_metadata.signals  # noqa: F401

