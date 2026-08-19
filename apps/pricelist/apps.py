from django.apps import AppConfig


class PricelistConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.pricelist'
    label = 'pricelist'
    verbose_name = 'Equipment price list'
