"""
Copy Xero credentials, auth settings, tenants, and tenant tokens from v3 database to v4.

Usage (run from v4 project with DJANGO_SETTINGS_MODULE=klikk_business_intelligence.settings.development):
  python manage.py copy_xero_credentials_from_v3
  python manage.py copy_xero_credentials_from_v3 --user mc@tremly.com   # target v4 user (email or username)

Requires DATABASES['v3'] in settings (e.g. development.py) pointing at klikk_bi_v3.
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroAuthSettings, XeroClientCredentials, XeroTenantToken


User = get_user_model()


class Command(BaseCommand):
    help = "Copy Xero auth settings, tenants, credentials and tenant tokens from v3 DB to v4."

    def add_arguments(self, parser):
        parser.add_argument(
            '--user',
            type=str,
            default='mc@tremly.com',
            help='V4 user email or username to attach credentials to (default: mc@tremly.com)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only show what would be copied, do not write to v4.',
        )

    def handle(self, *args, **options):
        user_ident = options['user']
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run: no changes will be written to v4.'))

        # Resolve v4 target user
        try:
            v4_user = User.objects.get(email=user_ident) if '@' in user_ident else User.objects.get(username=user_ident)
        except User.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"V4 user not found: {user_ident}"))
            return

        # 1) XeroAuthSettings from v3 -> v4 (typically one row)
        try:
            for obj in XeroAuthSettings.objects.using('v3').all():
                if dry_run:
                    self.stdout.write(f"Would create XeroAuthSettings: auth_url={obj.auth_url}")
                    continue
                existing = XeroAuthSettings.objects.first()
                if existing:
                    existing.access_token_url = obj.access_token_url
                    existing.refresh_url = obj.refresh_url
                    existing.auth_url = obj.auth_url
                    existing.save()
                    self.stdout.write(self.style.SUCCESS("Updated XeroAuthSettings"))
                else:
                    XeroAuthSettings.objects.create(
                        access_token_url=obj.access_token_url,
                        refresh_url=obj.refresh_url,
                        auth_url=obj.auth_url,
                    )
                    self.stdout.write(self.style.SUCCESS("Created XeroAuthSettings"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"XeroAuthSettings: {e}"))
            if not dry_run:
                return

        # 2) XeroTenant from v3 -> v4 (tenant_id is PK)
        try:
            for obj in XeroTenant.objects.using('v3').all():
                if dry_run:
                    self.stdout.write(f"Would create XeroTenant: {obj.tenant_id} ({obj.tenant_name})")
                    continue
                XeroTenant.objects.update_or_create(
                    tenant_id=obj.tenant_id,
                    defaults={'tenant_name': obj.tenant_name}
                )
                self.stdout.write(self.style.SUCCESS(f"Created/updated XeroTenant: {obj.tenant_id}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"XeroTenant: {e}"))
            if not dry_run:
                return

        # 3) XeroClientCredentials from v3 -> v4 (attach to v4_user)
        cred_id_map = {}  # v3 cred pk -> v4 cred instance
        try:
            for obj in XeroClientCredentials.objects.using('v3').all():
                if dry_run:
                    self.stdout.write(f"Would create XeroClientCredentials: client_id={obj.client_id}")
                    continue
                cred, created = XeroClientCredentials.objects.update_or_create(
                    user=v4_user,
                    client_id=obj.client_id,
                    defaults={
                        'client_secret': obj.client_secret,
                        'scope': obj.scope or [],
                        'tenant_tokens': obj.tenant_tokens or {},
                        'active': obj.active,
                    }
                )
                cred_id_map[obj.pk] = cred
                self.stdout.write(self.style.SUCCESS(f"Created/updated XeroClientCredentials: {obj.client_id}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"XeroClientCredentials: {e}"))
            if not dry_run:
                return

        # 4) XeroTenantToken from v3 -> v4 (map tenant by tenant_id, credentials by client_id)
        try:
            for obj in XeroTenantToken.objects.using('v3').select_related('tenant', 'credentials').all():
                if dry_run:
                    self.stdout.write(f"Would create XeroTenantToken: tenant={obj.tenant_id}, cred_id={obj.credentials_id}")
                    continue
                v4_tenant = XeroTenant.objects.get(tenant_id=obj.tenant.tenant_id)
                v4_cred = cred_id_map.get(obj.credentials_id)
                if not v4_cred:
                    v4_cred = XeroClientCredentials.objects.get(user=v4_user, client_id=obj.credentials.client_id)
                XeroTenantToken.objects.update_or_create(
                    tenant=v4_tenant,
                    credentials=v4_cred,
                    defaults={
                        'token': obj.token,
                        'refresh_token': obj.refresh_token,
                        'expires_at': obj.expires_at,
                    }
                )
                self.stdout.write(self.style.SUCCESS(f"Created/updated XeroTenantToken: {obj.tenant.tenant_id}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"XeroTenantToken: {e}"))

        self.stdout.write(self.style.SUCCESS("Done."))
