from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom User model extending Django's AbstractUser.

    This model will be extended later to support:
    - Login with Xero
    - Login with Google
    - User to XeroTenant relationships
    """

    class Role(models.TextChoices):
        STANDARD = 'standard', 'Standard'
        AUDITOR = 'auditor', 'Auditor'
        SERVICE_READONLY = 'service_readonly', 'Service (read-only)'

    # Access role. 'standard' keeps today's behaviour (full console).
    # 'auditor' is hard-gated by AuditorGateMiddleware to read-only access
    # on the /audit/ surface — external auditors get accounts with this role.
    # 'service_readonly' is the machine equivalent, gated by the same
    # middleware to the journal-cube surface the Excel add-in reads plus the
    # cube collaboration writes it needs — and nothing else. Its credential
    # lives on a laptop, so it must not be able to reach a Xero write or a
    # sync trigger even though it is a perfectly valid Django login.
    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.STANDARD,
    )

    # Set when an account is handed a temporary password (create_auditor).
    # AuditorGateMiddleware then 403s everything except the auth endpoints and
    # the change-password endpoint until the holder picks their own password —
    # a shared/emailed temporary credential must not stay usable indefinitely.
    must_change_password = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_auditor(self):
        return self.role == self.Role.AUDITOR

    @property
    def is_service_readonly(self):
        return self.role == self.Role.SERVICE_READONLY
    
    class Meta:
        db_table = 'users'
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['-date_joined']
    
    def __str__(self):
        return self.email or self.username

