from django.db import models
from django.conf import settings
from apps.xero.xero_core.models import XeroTenant


class XeroClientCredentials(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='xero_client_credentials', on_delete=models.CASCADE)
    client_id = models.CharField(max_length=100)
    client_secret = models.CharField(max_length=100)
    scope = models.JSONField(blank=True)
    token = models.JSONField(blank=True, null=True)  # Legacy: Store OAuth2 token (deprecated, use tenant_tokens instead)
    refresh_token = models.CharField(max_length=1000, blank=True, null=True)  # Legacy: deprecated, use tenant_tokens instead
    expires_at = models.DateTimeField(blank=True, null=True)  # Legacy: deprecated, use tenant_tokens instead
    tenant_tokens = models.JSONField(default=dict, blank=True)  # Store tenant-specific tokens: {tenant_id: {token, refresh_token, expires_at, connected_at}}
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"Credentials for {self.user}"
    
    def get_tenant_token_data(self, tenant_id):
        """Get token data for a specific tenant."""
        return self.tenant_tokens.get(tenant_id)
    
    def set_tenant_token_data(self, tenant_id, token_data, refresh_token=None, expires_at=None, connected_at=None):
        """Set token data for a specific tenant (creating the slot if absent)."""
        self._write_token_slots(
            [tenant_id], token_data,
            refresh_token=refresh_token, expires_at=expires_at, connected_at=connected_at,
        )

    def set_token_data_for_all_tenants(self, token_data, refresh_token=None, expires_at=None,
                                       tenant_id=None):
        """Write ONE app-wide token into every tenant slot, atomically.

        Xero's OAuth token set (access + refresh) is per-APPLICATION, not per
        tenant: the organisation is chosen per request via the Xero-Tenant-Id
        header. Storing a copy per tenant and refreshing them independently is
        what made the 2026-08-18/19 lockouts possible — Xero rotates the refresh
        token on every use, so tenant A's refresh silently invalidates the copy
        sitting in tenant B's slot ~30 minutes later.

        Every refresh therefore writes the new token to ALL slots in one
        transaction, so there is exactly one canonical refresh token per
        credentials row no matter which tenant's job did the refresh.
        """
        targets = list((self.tenant_tokens or {}).keys())
        if tenant_id and tenant_id not in targets:
            targets.append(tenant_id)
        self._write_token_slots(
            targets, token_data, refresh_token=refresh_token, expires_at=expires_at,
            sync_legacy_rows=True,
        )

    def _write_token_slots(self, tenant_ids, token_data, refresh_token=None, expires_at=None,
                           connected_at=None, sync_legacy_rows=False):
        """Read-modify-write the tenant_tokens blob under a row lock.

        `save(update_fields=['tenant_tokens'])` rewrites the WHOLE JSON column
        from an in-memory dict loaded at fetch time, so two processes updating
        different tenants would each clobber the other's slot (the lost-update
        that split the token family). Re-read the row inside
        SELECT ... FOR UPDATE before mutating so writes serialise.
        """
        from django.db import transaction
        from django.utils import timezone

        def _iso(value):
            return value.isoformat() if hasattr(value, 'isoformat') else value

        expires_at = _iso(expires_at)
        connected_at = _iso(connected_at)

        with transaction.atomic():
            locked = XeroClientCredentials.objects.select_for_update().get(pk=self.pk)
            tokens = locked.tenant_tokens or {}
            for tenant_id in tenant_ids:
                slot = tokens.setdefault(tenant_id, {})
                slot['token'] = token_data
                if refresh_token:
                    slot['refresh_token'] = refresh_token
                if expires_at:
                    slot['expires_at'] = expires_at
                if connected_at:
                    slot['connected_at'] = connected_at
                elif 'connected_at' not in slot:
                    slot['connected_at'] = timezone.now().isoformat()
            locked.tenant_tokens = tokens
            locked.save(update_fields=['tenant_tokens'])

            if sync_legacy_rows and refresh_token and expires_at:
                # The deprecated XeroTenantToken rows are only read as a fallback
                # when a JSON slot is missing; keep them on the same token so they
                # can never resurrect a rotated-away one.
                from dateutil import parser
                XeroTenantToken.objects.filter(credentials=locked).update(
                    token=token_data,
                    refresh_token=refresh_token,
                    expires_at=parser.parse(expires_at) if isinstance(expires_at, str) else expires_at,
                )

        # Keep the in-memory copy consistent with what was just persisted.
        self.tenant_tokens = tokens

    def get_all_tenant_ids(self):
        """Get list of all tenant IDs that have tokens."""
        return list(self.tenant_tokens.keys())


class XeroTenantToken(models.Model):
    tenant = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='tenant_tokens')
    credentials = models.ForeignKey('XeroClientCredentials', on_delete=models.CASCADE, related_name='xero_tenant_tokens')
    token = models.JSONField()  # Tenant-specific token
    refresh_token = models.CharField(max_length=1000)
    expires_at = models.DateTimeField()
    connected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('tenant', 'credentials')]

    def __str__(self):
        return f"Token for {self.tenant.tenant_name}"


class XeroAuthSettings(models.Model):
    access_token_url = models.CharField(max_length=255)
    refresh_url = models.CharField(max_length=255)
    auth_url = models.CharField(max_length=255)
