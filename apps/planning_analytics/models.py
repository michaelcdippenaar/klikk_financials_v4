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


class KPITarget(models.Model):
    """User-set performance targets for the Cost & Sustainability Cockpit.

    Targets are GOALS the user sets to challenge themselves — they are NOT TM1
    plan data (which lives in TM1) and NOT actuals (Postgres XeroTrailBalance /
    TM1 gl_src). They are a third, distinct datum: the bar to beat. Single home
    = Postgres. The cost-cut report overlays target vs actual and emits RAG.

    metric_key examples:
      "cost_cut.total"               — total recurring-cash cost for the entity/year
      "cost_cut.account.<account_id>" — a single leaf expense account
    (extensible to cashflow.*, debt.*, tax.*, sustainability.* as pillars land)
    """
    LOWER = "lower_is_better"
    HIGHER = "higher_is_better"
    DIRECTION_CHOICES = [(LOWER, "Lower is better"), (HIGHER, "Higher is better")]

    metric_key = models.CharField(max_length=200)
    entity_id = models.CharField(max_length=64, blank=True, default="")  # Xero tenant uuid; "" = org-wide
    period_year = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=300, blank=True, default="")
    target_value = models.DecimalField(max_digits=18, decimal_places=2)
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES, default=LOWER)
    amber_band_pct = models.DecimalField(max_digits=6, decimal_places=2, default=5)  # % tolerance for amber RAG
    note = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="kpi_targets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "KPI Target"
        verbose_name_plural = "KPI Targets"
        ordering = ["entity_id", "period_year", "metric_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["metric_key", "entity_id", "period_year"],
                name="uniq_kpi_target_metric_entity_year",
            )
        ]

    def __str__(self):
        return f"{self.metric_key} {self.entity_id or 'org'} {self.period_year} -> {self.target_value}"

    def rag(self, actual):
        """Red/Amber/Green vs actual. Returns one of 'green'|'amber'|'red'|'none'."""
        if actual is None:
            return "none"
        t = float(self.target_value)
        band = float(self.amber_band_pct) / 100.0
        a = float(actual)
        if self.direction == self.LOWER:
            if a <= t:
                return "green"
            if a <= t * (1 + band):
                return "amber"
            return "red"
        else:
            if a >= t:
                return "green"
            if a >= t * (1 - band):
                return "amber"
            return "red"
