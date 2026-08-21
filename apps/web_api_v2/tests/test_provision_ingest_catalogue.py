from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.web_api_v2.models import IngestSourceJobDefinition, UserEntityCapability
from apps.xero.xero_core.models import XeroTenant


class ProvisionIngestCatalogueTests(TestCase):
    def test_new_entity_is_provisioned_without_execution_grants(self):
        entity = XeroTenant.objects.create(tenant_id='new-catalogue', tenant_name='New Catalogue')
        self.assertEqual(IngestSourceJobDefinition.objects.filter(entity=entity).count(), 8)
        self.assertFalse(UserEntityCapability.objects.exists())

    def test_command_defaults_to_dry_run_and_requires_apply(self):
        entity = XeroTenant.objects.create(tenant_id='dry-run-catalogue', tenant_name='Dry Run')
        IngestSourceJobDefinition.objects.filter(entity=entity).delete()
        output = StringIO()
        call_command('provision_ingest_catalogue', entity_id=entity.pk, stdout=output)
        self.assertIn('DRY RUN', output.getvalue())
        self.assertIn('No rows changed', output.getvalue())
        self.assertFalse(IngestSourceJobDefinition.objects.filter(entity=entity).exists())

        output = StringIO()
        call_command(
            'provision_ingest_catalogue', entity_id=entity.pk, apply=True, stdout=output,
        )
        self.assertIn('APPLIED', output.getvalue())
        self.assertEqual(IngestSourceJobDefinition.objects.filter(entity=entity).count(), 8)
        self.assertFalse(UserEntityCapability.objects.exists())
