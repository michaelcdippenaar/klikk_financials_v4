"""
Upsert the 2025-12-03 rate card (apps/pricelist/seed_data.py) into the price list.

    python manage.py seed_pricelist                 # all items + LIST prices + Aurras TRADE prices
    python manage.py seed_pricelist --only DB-V10P,DB-D80
    python manage.py seed_pricelist --dry-run       # report what would change, write nothing

IDEMPOTENT:
  * items are ``update_or_create``-d by code; an item MC has deactivated stays
    deactivated (``active`` is never overwritten on update);
  * prices go through ``services.add_price`` — the same ``valid_from`` updates
    the row in place, so re-running is a no-op;
  * the Profit Center tracking option and the supplier-bill purchase line are
    resolved by lookup against the local Xero mirror; misses are WARNINGS, not
    failures (the command must work on an empty test DB);
  * the Aurras contact is resolved by id, then by name; if absent the TRADE
    rows are skipped with a warning.

Never calls Xero.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.pricelist.models import PriceListItem, PriceListPrice
from apps.pricelist.seed_data import (
    AURRAS_CONTACT_ID, AURRAS_CONTACT_NAME, ITEMS, RATE_CARD_VALID_FROM, SOURCE, TRADE_NOTE, TRADE_PRICES,
)
from apps.pricelist.services import add_price
from apps.xero.xero_data.models import XeroInvoiceLineItem
from apps.xero.xero_metadata.models import XeroContacts, XeroTracking

SET_BY = 'mc'


class Command(BaseCommand):
    help = 'Upsert the seed equipment rate card (items + effective-dated prices). Idempotent.'

    def add_arguments(self, parser):
        parser.add_argument('--only', help='Comma-separated item codes to seed.')
        parser.add_argument('--dry-run', action='store_true', help='Report only; write nothing.')

    # ------------------------------------------------------------------ lookups
    def _tracking_option_id(self, option_name, cache):
        """Profit Center option name -> option_id ('' if not in the mirror)."""
        if not option_name:
            return ''
        if option_name not in cache:
            row = XeroTracking.objects.filter(name='Profit Center', option=option_name).first()
            cache[option_name] = row.option_id if row else ''
        return cache[option_name]

    def _purchase_line(self, spec):
        """(invoice_number, description) -> XeroInvoiceLineItem or None. Exact first, then prefix."""
        if not spec:
            return None
        invoice_number, description = spec
        qs = XeroInvoiceLineItem.objects.filter(invoice__invoice_number=invoice_number)
        line = qs.filter(description=description).order_by('id').first()
        if line is None:
            # Descriptions get re-synced with trailing whitespace / casing tweaks; the first 40
            # chars are stable enough to identify the line on a short supplier bill.
            line = qs.filter(description__istartswith=description[:40]).order_by('id').first()
        return line

    def _aurras(self):
        return (XeroContacts.objects.filter(contacts_id=AURRAS_CONTACT_ID).first()
                or XeroContacts.objects.filter(name=AURRAS_CONTACT_NAME).first())

    # ------------------------------------------------------------------ handle
    def handle(self, *args, **opts):
        only = {c.strip().upper() for c in (opts.get('only') or '').split(',') if c.strip()}
        dry_run = bool(opts.get('dry_run'))
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — nothing will be written'))
        self.stdout.write(f'source: {SOURCE}; rate card valid_from {RATE_CARD_VALID_FROM}')

        stats = {
            'items_created': 0, 'items_updated': 0,
            'prices_created': 0, 'prices_updated': 0, 'prices_closed': 0,
            'tracking_resolved': 0, 'tracking_missed': 0,
            'lines_resolved': 0, 'lines_missed': 0,
            'trade_skipped': 0,
        }
        tracking_cache = {}

        # The whole seed is one transaction; --dry-run rolls it back at the end so we still
        # exercise every lookup and add_price validation on real data.
        with transaction.atomic():
            items_by_code = {}
            for spec in ITEMS:
                code = spec['code']
                if only and code not in only:
                    continue

                option_id = self._tracking_option_id(spec['tracking'], tracking_cache)
                if spec['tracking']:
                    if option_id:
                        stats['tracking_resolved'] += 1
                    else:
                        stats['tracking_missed'] += 1
                        self.stderr.write(self.style.WARNING(
                            f"{code}: Profit Center option {spec['tracking']!r} not in xero_metadata_xerotracking — "
                            f'leaving xero_tracking_option_id blank'))

                line = self._purchase_line(spec['purchase_line'])
                if spec['purchase_line']:
                    if line is not None:
                        stats['lines_resolved'] += 1
                    else:
                        stats['lines_missed'] += 1
                        inv, desc = spec['purchase_line']
                        self.stderr.write(self.style.WARNING(
                            f'{code}: purchase line {inv} / {desc!r} not in xero_data_xeroinvoicelineitem — '
                            f'leaving xero_purchase_line NULL'))

                defaults = {
                    'name': spec['name'], 'category': spec['category'], 'unit': spec['unit'],
                    'qty_owned': spec['qty_owned'], 'xero_account_code': spec['account'],
                    'notes': spec.get('notes', ''),
                }
                # Only attach links we actually resolved — never blank out a tracking option or
                # purchase line MC linked by hand on an earlier run.
                if option_id:
                    defaults['xero_tracking_option_id'] = option_id
                if line is not None:
                    defaults['xero_purchase_line'] = line
                item, created = PriceListItem.objects.update_or_create(
                    code=code,
                    defaults=defaults,
                    # ``active`` only on create — an item MC deactivated must stay deactivated.
                    create_defaults={**defaults, 'active': True},
                )
                items_by_code[code] = item
                stats['items_created' if created else 'items_updated'] += 1

                pre_existing = self._lane_has(item, 'LIST', None)
                _row, closed = add_price(item, price=spec['price'], valid_from=RATE_CARD_VALID_FROM,
                                         price_type='LIST', customer=None, note=SOURCE, set_by=SET_BY)
                self._count_price(stats, pre_existing, closed)
                self.stdout.write(f"{'created' if created else 'updated'} {code:<15} LIST R{spec['price']:>9}"
                                  f"  tracking={'ok' if option_id else '-'}  line={'ok' if line else '-'}")

            # Aurras TRADE lane -----------------------------------------------------------
            aurras = self._aurras()
            if aurras is None:
                n = sum(1 for t in TRADE_PRICES if not only or t['code'] in only)
                stats['trade_skipped'] += n
                if n:
                    self.stderr.write(self.style.WARNING(
                        f'Aurras contact ({AURRAS_CONTACT_ID} / {AURRAS_CONTACT_NAME!r}) not in '
                        f'xero_metadata_xerocontacts — skipping {n} TRADE price(s)'))
            else:
                for t in TRADE_PRICES:
                    code = t['code']
                    if only and code not in only:
                        continue
                    item = items_by_code.get(code) or PriceListItem.objects.filter(code=code).first()
                    if item is None:
                        stats['trade_skipped'] += 1
                        self.stderr.write(self.style.WARNING(f'{code}: item missing, TRADE price skipped'))
                        continue
                    pre_existing = self._lane_has(item, 'TRADE', aurras)
                    _row, closed = add_price(item, price=t['price'], valid_from=RATE_CARD_VALID_FROM,
                                             price_type='TRADE', customer=aurras, note=TRADE_NOTE, set_by=SET_BY)
                    self._count_price(stats, pre_existing, closed)
                    self.stdout.write(f"{'':8}{code:<15} TRADE R{t['price']:>8}  customer={aurras.name}")

            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f"items: created={stats['items_created']} updated={stats['items_updated']} | "
            f"prices: created={stats['prices_created']} updated={stats['prices_updated']} "
            f"closed_previous={stats['prices_closed']} trade_skipped={stats['trade_skipped']} | "
            f"tracking: resolved={stats['tracking_resolved']} missed={stats['tracking_missed']} | "
            f"purchase lines: resolved={stats['lines_resolved']} missed={stats['lines_missed']}"
            + ('  [DRY RUN — rolled back]' if dry_run else '')
        ))

    @staticmethod
    def _count_price(stats, pre_existing, closed):
        """add_price returns the same shape for insert and in-place update, so the caller checks
        beforehand whether a row with that valid_from already sat in the lane."""
        stats['prices_updated' if pre_existing else 'prices_created'] += 1
        if closed is not None:
            stats['prices_closed'] += 1

    @staticmethod
    def _lane_has(item, price_type, customer):
        return PriceListPrice.objects.filter(
            item=item, price_type=price_type, customer=customer, valid_from=RATE_CARD_VALID_FROM,
        ).exists()
