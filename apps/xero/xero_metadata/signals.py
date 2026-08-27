"""
When Xero accounts or contacts change, request that the AI agent's glossary docs be refreshed
so the vectorized model stays up to date (account names/purpose, Suppliers vs Customers).
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import XeroAccount, XeroContacts


def _request_glossary_refresh(tenant_id):
    """Record which Xero tenant changed, on the singleton request row.

    tenant_id is XeroTenant's primary key — a varchar(100) Xero org GUID, NOT a
    number. It used to be written into GlossaryRefreshRequest.organisation_id,
    an IntegerField, which raised ValueError out of post_save and took the
    caller's XeroAccount/XeroContacts save() down with it.
    """
    from apps.ai_agent.models import GlossaryRefreshRequest
    req, _ = GlossaryRefreshRequest.objects.get_or_create(
        pk=1,
        defaults={'tenant_id': tenant_id},
    )
    req.requested_at = timezone.now()
    req.tenant_id = tenant_id
    req.save(update_fields=['requested_at', 'tenant_id'])


@receiver(post_save, sender=XeroAccount)
def request_glossary_refresh_on_account(sender, instance, **kwargs):
    _request_glossary_refresh(instance.organisation_id)


@receiver(post_save, sender=XeroContacts)
def request_glossary_refresh_on_contact(sender, instance, **kwargs):
    _request_glossary_refresh(instance.organisation_id)
