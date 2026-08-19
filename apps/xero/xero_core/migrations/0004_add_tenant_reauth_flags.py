"""Track tenants whose Xero refresh token is dead.

Schema: reauth_required / reauth_reason / reauth_flagged_at on XeroTenant.

Data: flags the two tenants whose refresh tokens were already dead on
2026-08-19 (Xero returned 400 invalid_grant for both). Without this the hourly
out-of-sync job keeps retrying them every hour forever, burning API budget and
flooding the logs. MC clears the flag by re-authorizing in the console.
"""
from django.db import migrations, models


# (tenant_id, tenant_name) known dead at the time this migration was written.
DEAD_TENANTS = [
    ('0415e61e-f78c-4216-ac54-7933a6f63a5d', 'Tremly (Pty) Ltd'),
    ('27806be4-62dd-4c50-9eb9-c8b79231f6a1', 'Dippenaar Family'),
]

REASON = (
    'Xero token endpoint returned 400 invalid_grant (refresh token dead) as at '
    '2026-08-19. Re-authorize this tenant in Setup -> Credentials -> Xero.'
)


def flag_dead_tenants(apps, schema_editor):
    from django.utils import timezone
    XeroTenant = apps.get_model('xero_core', 'XeroTenant')
    now = timezone.now()
    for tenant_id, _name in DEAD_TENANTS:
        XeroTenant.objects.filter(tenant_id=tenant_id).update(
            reauth_required=True,
            reauth_reason=REASON,
            reauth_flagged_at=now,
        )


def unflag_dead_tenants(apps, schema_editor):
    XeroTenant = apps.get_model('xero_core', 'XeroTenant')
    XeroTenant.objects.filter(
        tenant_id__in=[t for t, _ in DEAD_TENANTS]
    ).update(reauth_required=False, reauth_reason='', reauth_flagged_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('xero_core', '0003_add_fiscal_year_start_month'),
    ]

    operations = [
        migrations.AddField(
            model_name='xerotenant',
            name='reauth_required',
            field=models.BooleanField(
                default=False,
                help_text='Xero refresh token is dead; skip all scheduled syncs until re-authorized in the console.',
            ),
        ),
        migrations.AddField(
            model_name='xerotenant',
            name='reauth_reason',
            field=models.TextField(
                blank=True, default='',
                help_text='Why re-authorization is needed (last token-refresh error from Xero).',
            ),
        ),
        migrations.AddField(
            model_name='xerotenant',
            name='reauth_flagged_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='When reauth_required was first set (cleared on successful re-authorization).',
            ),
        ),
        migrations.RunPython(flag_dead_tenants, unflag_dead_tenants),
    ]
