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


class CostBehaviour(models.Model):
    """Cost-behaviour classification (fixed/variable/semi-variable/non-controllable)
    per expense account, for the Cost & Sustainability Cockpit.

    Per the CFO's design: the *default* is seeded once from the management-accounting
    classification (source='cfo_seed'); MC can re-tag any account from the cockpit
    (source='user_override') without a redeploy. Resolved value = this row (override
    wins over seed because an override write flips source + stamps updated_by).

    NOTE (deferred half of the CFO data-model rec): the same tag should ALSO be seeded
    as a TM1 `cost_behaviour` account attribute when forecast-flexing-by-behaviour is
    built (so TM1 planning rules can reflood a flexed budget by behaviour). For now the
    Postgres row is the single authoritative classification consumed by the report.
    """
    FIXED = "fixed"
    VARIABLE = "variable"
    SEMI = "semi_variable"
    NON_CONTROLLABLE = "non_controllable"
    BEHAVIOUR_CHOICES = [
        (FIXED, "Fixed"), (VARIABLE, "Variable"),
        (SEMI, "Semi-variable"), (NON_CONTROLLABLE, "Non-controllable"),
    ]
    SEED = "cfo_seed"
    OVERRIDE = "user_override"

    account_key = models.CharField(max_length=120, unique=True)  # e.g. "kl_HH--EM01/1"
    behaviour = models.CharField(max_length=20, choices=BEHAVIOUR_CHOICES)
    driver = models.CharField(max_length=200, blank=True, default="")
    # Cuttability tier (CFO axis, distinct from behaviour): T0 not-a-target /
    # below-the-line, T1 quick-win, T2 behavioural, T3 discretionary,
    # T4 renegotiable, T5 structural.
    TIERS = [("T0", "Not a target"), ("T1", "Quick win"), ("T2", "Behavioural"),
             ("T3", "Discretionary"), ("T4", "Renegotiable"), ("T5", "Structural")]
    cuttability = models.CharField(max_length=2, choices=TIERS, default="T2")
    # False = below-the-line (tax / finance / statutory-derivative / contra);
    # excluded from the addressable cost-cut total.
    is_addressable = models.BooleanField(default=True)
    # Manageable cost = the user's top cost-cutting opportunities: addressable AND
    # reducible THIS period (tier T1/T2/T3 by default — quick-win/behavioural/
    # discretionary). Editable so MC can curate his hit-list independent of tier.
    is_manageable = models.BooleanField(default=False)
    source = models.CharField(max_length=20, default=SEED)
    note = models.TextField(blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="cost_behaviours",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cost Behaviour"
        verbose_name_plural = "Cost Behaviours"
        ordering = ["account_key"]

    def __str__(self):
        return f"{self.account_key} -> {self.behaviour} ({self.source})"


class EntityPlanningTarget(models.Model):
    """Which Planning Analytics destination an entity submits to.

    TM1ServerConfig and TM1ProcessConfig are global: they describe how to reach
    the TM1 server, not who may send what to it. Without a per-entity binding,
    "submit this entity's close to Planning Analytics" has no defined
    destination, which is why the V2 connection reports Planning Analytics as
    unavailable until an entity-bound destination is approved.

    This model is that binding, and nothing more. It deliberately holds no
    connection details of its own: the server row owns those, TM1 is reachable
    only from inside the network, and the presentation-safe read for the
    browser exposes names alone. A destination is usable only once someone has
    approved it — created is not approved.
    """

    entity = models.ForeignKey(
        'xero_core.XeroTenant',
        on_delete=models.CASCADE,
        related_name='planning_targets',
        help_text='The Xero tenant whose data this destination receives.',
    )
    server = models.ForeignKey(
        TM1ServerConfig,
        on_delete=models.PROTECT,
        related_name='entity_targets',
        help_text='TM1 server. Its credentials never leave the server side.',
    )
    display_name = models.CharField(
        max_length=200,
        help_text='Human name for the destination, e.g. "PA Production · Finance".',
    )
    workspace = models.CharField(
        max_length=300,
        help_text='Cube or application path within TM1, e.g. "Group Finance / General Ledger".',
    )
    default_scenario = models.CharField(max_length=120, blank=True, default='')
    default_version = models.CharField(max_length=120, blank=True, default='')

    active = models.BooleanField(default=True)
    # Separate from `active` on purpose. A binding can exist, be switched on,
    # and still not be approved to receive an entity's financial data; those
    # are different questions and conflating them is how data reaches the wrong
    # destination.
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    approval_note = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Entity Planning Target'
        verbose_name_plural = 'Entity Planning Targets'
        ordering = ['entity_id', 'display_name']
        constraints = [
            # One active destination per entity: two would make "submit this
            # entity" ambiguous, and an ambiguous destination is a wrong one.
            models.UniqueConstraint(
                fields=['entity'],
                condition=models.Q(active=True),
                name='pa_one_active_target_per_entity',
            ),
        ]
        indexes = [
            models.Index(fields=['entity', 'active'], name='pa_target_entity_active_idx'),
        ]

    def __str__(self):
        return f'{self.entity_id}: {self.display_name}'

    @property
    def approved(self):
        return self.approved_at is not None

    @classmethod
    def for_entity(cls, entity_id):
        return (
            cls.objects.select_related('server')
            .filter(entity_id=entity_id, active=True)
            .first()
        )
