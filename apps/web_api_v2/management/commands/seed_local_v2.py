import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.user.models import User
from apps.web_api_v2.models import (
    IngestProcessAuditEvent,
    IngestProcessRun,
    IngestSourceJobDefinition,
    UserEntityCapability,
    UserEntityMembership,
    ViewerPreference,
)
from apps.web_api_v2.services.ingest_registry import PROCESS_DEFINITIONS
from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken
from apps.xero.xero_core.models import XeroTenant


SYNTHETIC_USERNAME = "local-v2-reader"
SYNTHETIC_ENTITY_ID = "local-v2-entity-0001"
SYNTHETIC_ENTITY_NAME = "Local Synthetic Entity"


class Command(BaseCommand):
    help = "Create the deterministic, VIEW-only local V2 fixture."

    def _guard_environment(self):
        if not getattr(settings, "LOCAL_V2_SAFE_MODE", False):
            raise CommandError("seed_local_v2 is restricted to LOCAL_V2_SAFE_MODE.")
        database_name = settings.DATABASES["default"]["NAME"]
        if not str(database_name).startswith("klikk_v2_local"):
            raise CommandError("Refusing to seed a non-local-v2 database.")
        password = os.environ.get("LOCAL_V2_SYNTHETIC_PASSWORD", "")
        if len(password) < 16:
            raise CommandError(
                "LOCAL_V2_SYNTHETIC_PASSWORD must contain at least 16 characters."
            )
        return password

    @transaction.atomic
    def handle(self, *args, **options):
        password = self._guard_environment()

        foreign_entities = XeroTenant.objects.exclude(pk=SYNTHETIC_ENTITY_ID)
        foreign_users = User.objects.exclude(username=SYNTHETIC_USERNAME)
        if foreign_entities.exists() or foreign_users.exists():
            raise CommandError(
                "Local fixture database contains non-synthetic identities; purge it first."
            )
        if XeroClientCredentials.objects.exists() or XeroTenantToken.objects.exists():
            raise CommandError("OAuth/source credentials are forbidden in the local fixture.")
        if IngestProcessRun.objects.exists() or IngestProcessAuditEvent.objects.exists():
            raise CommandError("Process runs and audit events are forbidden in the local fixture.")

        user, _created = User.objects.update_or_create(
            username=SYNTHETIC_USERNAME,
            defaults={
                "email": "local-v2-reader@example.invalid",
                "first_name": "Local",
                "last_name": "Reader",
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
            },
        )
        user.set_password(password)
        user.save(update_fields=("password", "updated_at"))

        entity, _created = XeroTenant.objects.update_or_create(
            tenant_id=SYNTHETIC_ENTITY_ID,
            defaults={
                "tenant_name": SYNTHETIC_ENTITY_NAME,
                "tracking_category_1_id": None,
                "tracking_category_2_id": None,
                "fiscal_year_start_month": 7,
                "reauth_required": False,
                "reauth_reason": "",
                "reauth_flagged_at": None,
            },
        )
        membership, _created = UserEntityMembership.objects.update_or_create(
            user=user,
            entity=entity,
            defaults={"role": UserEntityMembership.Role.VIEWER, "active": True},
        )
        UserEntityCapability.objects.filter(membership=membership).delete()
        ViewerPreference.objects.update_or_create(
            user=user,
            defaults={"default_entity": entity, "default_financial_year": 2026},
        )

        expected_keys = {key for key in PROCESS_DEFINITIONS if key != "standard-sync"}
        IngestSourceJobDefinition.objects.filter(entity=entity).exclude(
            key__in=expected_keys
        ).delete()
        for key in sorted(expected_keys):
            definition = PROCESS_DEFINITIONS[key]
            IngestSourceJobDefinition.objects.update_or_create(
                entity=entity,
                key=key,
                defaults={
                    "source_family": IngestSourceJobDefinition.SourceFamily.XERO,
                    "label": definition["display_name"],
                    "required": definition["required"],
                    "configuration_state": (
                        IngestSourceJobDefinition.ConfigurationState.NOT_CONFIGURED
                    ),
                    "supported_operations": [],
                    "read_capabilities": ["VIEW_STATUS"],
                    "active": True,
                },
            )

        if UserEntityCapability.objects.exists():
            raise CommandError("Execution capability rows are forbidden in the local fixture.")

        self.stdout.write(
            self.style.SUCCESS(
                "Local V2 synthetic fixture ready: 1 user, 1 entity, "
                "VIEW_FINANCIALS only, 0 execution grants."
            )
        )
