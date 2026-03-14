from django.conf import settings
from django.db import models


class UserTM1Credentials(models.Model):
    """Per-user TM1 credentials — links a Django user to their TM1 identity."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='tm1_credentials',
    )
    tm1_username = models.CharField(max_length=200)
    tm1_password = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'User TM1 Credentials'
        verbose_name_plural = 'User TM1 Credentials'

    def __str__(self):
        return f'{self.user} → {self.tm1_username}'


class TM1ServerConfig(models.Model):
    """Active TM1 server connection details (singleton-ish: one active row)."""
    base_url = models.URLField(max_length=500)
    username = models.CharField(max_length=200, blank=True, default='')
    password = models.CharField(max_length=200, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'TM1 Server Config'
        verbose_name_plural = 'TM1 Server Configs'

    def __str__(self):
        return f'{self.base_url} ({"active" if self.is_active else "inactive"})'

    def save(self, *args, **kwargs):
        if self.is_active:
            TM1ServerConfig.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first()


class TM1ProcessConfig(models.Model):
    """A TI process that can be executed via the pipeline."""
    process_name = models.CharField(max_length=300)
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    parameters = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'TM1 Process Config'
        verbose_name_plural = 'TM1 Process Configs'

    def __str__(self):
        return f'{self.process_name} ({"enabled" if self.enabled else "disabled"})'
