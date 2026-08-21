from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.xero.xero_core.models import XeroTenant


@receiver(post_save, sender=XeroTenant, dispatch_uid='web_api_v2_provision_ingest_catalogue')
def provision_ingest_catalogue_for_new_entity(sender, instance, created, **kwargs):
    if not created:
        return
    from apps.web_api_v2.services.ingest_registry import provision_definition_defaults

    provision_definition_defaults(instance)
