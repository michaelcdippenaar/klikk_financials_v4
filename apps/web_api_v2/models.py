from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.xero.xero_core.models import XeroTenant


class UserEntityMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = 'OWNER', 'Owner'
        ADMIN = 'ADMIN', 'Administrator'
        REVIEWER = 'REVIEWER', 'Reviewer'
        VIEWER = 'VIEWER', 'Viewer'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='entity_memberships',
    )
    entity = models.ForeignKey(
        XeroTenant,
        on_delete=models.CASCADE,
        related_name='user_memberships',
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'entity'),
                name='web_api_v2_unique_user_entity',
            ),
        ]
        indexes = [
            models.Index(
                fields=('user', 'active'),
                name='web_api_v2_user_active_idx',
            ),
        ]
        ordering = ('entity__tenant_name', 'entity_id')

    def __str__(self):
        return f'{self.user} -> {self.entity} ({self.role})'


class ViewerPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='web_api_v2_preferences',
    )
    default_entity = models.ForeignKey(
        XeroTenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    default_financial_year = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=(MinValueValidator(1900), MaxValueValidator(9999)),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Web preferences for {self.user}'
