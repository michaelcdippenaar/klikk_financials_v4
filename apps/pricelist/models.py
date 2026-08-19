"""
Equipment rate card — ``pricelist_pricelistitem`` + ``pricelist_pricelistprice``.

* ``PriceListItem``  — one hire-able asset / SKU (code, name, category, unit,
                       qty owned) plus the read-only links back into the Xero
                       mirror: the account it was capitalised to, the Profit
                       Center tracking option it earns under, and the supplier
                       bill line it was bought on.
* ``PriceListPrice`` — effective-dated prices. A price lives in a "lane" =
                       (item, price_type, customer); within a lane rows are
                       contiguous and non-overlapping, ``valid_to IS NULL``
                       marks the current row. Customer-specific rows override
                       the open LIST row (see ``services.get_price``).

Money is ex-VAT ZAR, ``Decimal`` in Python, serialised as 2-dp strings by the
services layer. This app never writes to Xero; the FKs point at mirror tables
and are SET_NULL so a re-sync that rebuilds the mirror cannot cascade-delete
the rate card.
"""
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField
from django.contrib.postgres.fields.ranges import RangeOperators
from django.db import models
from django.db.models import F, Func, Q, Value

CATEGORY_CHOICES = [
    ('PA', 'PA tops / subs'),
    ('AMP', 'Amplifiers'),
    ('DJ', 'DJ gear'),
    ('PROJECTOR', 'Projectors'),
    ('LENS', 'Projector lenses'),
    ('LIGHTING', 'Lighting'),
    ('STAGING', 'Staging'),
    ('RIGGING', 'Rigging'),
    ('CABLING', 'Cabling & consumables'),
    ('OTHER', 'Other'),
]
CATEGORY_VALUES = tuple(c for c, _ in CATEGORY_CHOICES)

UNIT_CHOICES = [
    ('DAY', 'Per day'),
    ('EVENT', 'Per event'),
    ('WEEK', 'Per week'),
    ('SEASON', 'Per season'),
]
UNIT_VALUES = tuple(u for u, _ in UNIT_CHOICES)

PRICE_TYPE_CHOICES = [
    ('LIST', 'List'),
    ('TRADE', 'Trade'),
    ('SPECIAL', 'Special'),
]
PRICE_TYPE_VALUES = tuple(p for p, _ in PRICE_TYPE_CHOICES)


class PriceListItem(models.Model):
    code = models.CharField(max_length=32, unique=True, db_index=True,
                            help_text='Short slug, e.g. DB-V10P. Upper-cased on save.')
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default='OTHER', db_index=True)
    unit = models.CharField(max_length=16, choices=UNIT_CHOICES, default='DAY')
    qty_owned = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True, default='')
    active = models.BooleanField(default=True, db_index=True)
    xero_account_code = models.CharField(
        max_length=20, blank=True, default='',
        help_text='Xero account the asset was capitalised to, e.g. EE-SE01 Event Equipment @ Cost.',
    )
    xero_tracking_option_id = models.CharField(
        max_length=64, blank=True, default='',
        help_text='xero_metadata_xerotracking.option_id of the Profit Center this asset earns under.',
    )
    xero_purchase_line = models.ForeignKey(
        'xero_data.XeroInvoiceLineItem', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='pricelist_items',
        help_text='The supplier-bill line the asset was bought on.',
    )
    xero_fixed_asset_id = models.UUIDField(
        null=True, blank=True,
        help_text="Xero Fixed Assets register AssetId. TODO: not populated — the Xero app has no "
                  "'assets.read' scope and there is no local fixed-asset mirror table. See docs/pricelist.md.",
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'code']
        verbose_name = 'Price list item'

    def save(self, *args, **kwargs):
        # Codes are the public identifier in URLs and the seed file; normalise once here so
        # 'db-v10p', ' DB-V10P ' and 'DB-V10P' are the same row.
        self.code = (self.code or '').strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.code} — {self.name}'


class PriceListPrice(models.Model):
    item = models.ForeignKey(PriceListItem, on_delete=models.CASCADE, related_name='prices')
    price = models.DecimalField(max_digits=12, decimal_places=2, help_text='Ex VAT, ZAR.')
    valid_from = models.DateField(db_index=True)
    valid_to = models.DateField(null=True, blank=True, help_text='Inclusive last day; NULL = still current.')
    price_type = models.CharField(max_length=16, choices=PRICE_TYPE_CHOICES, default='LIST', db_index=True)
    customer = models.ForeignKey(
        'xero_metadata.XeroContacts', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='pricelist_prices', help_text='NULL = applies to everyone.',
    )
    note = models.TextField(blank=True, default='')
    set_by = models.CharField(max_length=64, blank=True, default='', help_text="'mc' / 'claude-mcp' / username")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['item', '-valid_from', '-id']
        verbose_name = 'Price list price'
        constraints = [
            # ``condition=`` (not ``check=``): the project pins Django>=5.1 where ``check`` is
            # deprecated and warns on every import.
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F('valid_from')),
                name='pricelist_price_valid_range',
            ),
            # Only ONE open/general LIST price may be in force for an item on any given day.
            # daterange(valid_from, valid_to, '[]') with valid_to NULL is unbounded above in
            # Postgres — exactly the "still current" semantics we want, so the open row
            # collides with anything dated after its valid_from until it is closed.
            # Customer-specific and TRADE/SPECIAL lanes are deliberately NOT covered: they are
            # overrides and the resolution order in services.get_price picks the newest.
            # Needs the btree_gist extension (the migration creates it) because '=' on a
            # bigint FK inside a GiST index is not available without it.
            ExclusionConstraint(
                name='pricelist_no_overlapping_list_price',
                expressions=[
                    (
                        Func(F('valid_from'), F('valid_to'), Value('[]'),
                             function='daterange', output_field=DateRangeField()),
                        RangeOperators.OVERLAPS,
                    ),
                    ('item', RangeOperators.EQUAL),
                ],
                condition=Q(price_type='LIST', customer__isnull=True),
            ),
        ]

    def __str__(self):
        who = self.customer_id or 'all'
        until = self.valid_to.isoformat() if self.valid_to else 'open'
        return f'{self.item_id}:{self.price_type}/{who} R{self.price} {self.valid_from}→{until}'
