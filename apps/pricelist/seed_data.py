"""
Seed content for the equipment price list — the 2025-12-03 rate card.

Module-level constants only; NO database access here (the management command
``seed_pricelist`` resolves tracking options, purchase lines and the Aurras
contact against the local Xero mirror at run time).

Provenance — every value below was verified against the live Xero mirror on
2026-08-19; do NOT "correct" them from memory:

  * Day rates come from the Dec-2025 Aurras invoices INV-0955 / INV-1037 /
    INV-1038 and INV-0969 (the 20% trade discount on the Aurras lines gives
    the TRADE_PRICES; the list rate is the undiscounted unit amount).
  * Profit Center: DJ gear is invoiced under the 'Equipment Rental - Sound
    Equipment' Profit Center (verified on the INV-0955 lines) — there is NO
    'DJ Gear' Profit Center, so CDJ/DJM rows carry TRACKING_SOUND.
  * Capitalisation account: the Epson EB-PU2220B bill line is coded 722
    'Electrical Equipment - @ cost' while every other asset bill line is
    EE-SE01 'Event Equipment @ Cost'. Recorded as-is; whether 722 should be
    reclassified is a year-end question for the accountant, not for this seed.
  * Purchase lines are (supplier invoice_number, exact line description) pairs
    on ``xero_data_xeroinvoicelineitem`` — the command falls back to an
    ``istartswith`` match on the first 40 chars if the exact description misses.
  * Tracking option ids are NOT hard-coded (they are per-tenant UUIDs that would
    change if the category were ever rebuilt); the OPTION NAME is recorded and
    resolved by ``XeroTracking.objects.filter(name='Profit Center', option=...)``.
  * TOTEM-25 qty_owned is a placeholder — MC has not confirmed the count.
"""
from datetime import date

SOURCE = 'seed:2025-12-03 rate card (Xero INV-0955 / INV-1037 / INV-1038 / INV-0969)'
RATE_CARD_VALID_FROM = date(2025, 12, 3)

# 'AURRAS GROUP (PTY) LTD' — verified in xero_metadata_xerocontacts.
AURRAS_CONTACT_ID = '1f4a93c8-b49a-46da-a7fc-25bafd5fb2b9'
AURRAS_CONTACT_NAME = 'AURRAS GROUP (PTY) LTD'

# Profit Center option NAMES (resolved to option_id at seed time; blank if not found).
TRACKING_SOUND = 'Equipment Rental - Sound Equipment'
TRACKING_PROJ = 'Equipment Rental - Projector'
TRACKING_STAGE = 'Equipment Rental - Stage and Structures'

ACCOUNT_EVENT_EQUIPMENT = 'EE-SE01'   # Event Equipment @ Cost
ACCOUNT_ELECTRICAL = '722'            # Electrical Equipment - @ cost (Epson only — see module docstring)

# Each entry: code, name, category, unit, qty_owned, list price (ex VAT, str), tracking option name,
# capitalisation account code, purchase_line (invoice_number, exact description) or None, notes.
ITEMS = [
    {
        'code': 'DB-V10P', 'name': 'd&b V10P point-source top', 'category': 'PA', 'unit': 'DAY',
        'qty_owned': 2, 'price': '850.00', 'tracking': TRACKING_SOUND, 'account': ACCOUNT_EVENT_EQUIPMENT,
        'purchase_line': None, 'notes': '',
    },
    {
        'code': 'DB-VGSUB', 'name': 'd&b V-GSUB subwoofer', 'category': 'PA', 'unit': 'DAY',
        'qty_owned': 4, 'price': '850.00', 'tracking': TRACKING_SOUND, 'account': ACCOUNT_EVENT_EQUIPMENT,
        'purchase_line': ('5973', 'D&B V-GSUB SUBWOOFER - NL4'), 'notes': '',
    },
    {
        'code': 'DB-D40', 'name': 'd&b D40 amplifier', 'category': 'AMP', 'unit': 'DAY',
        'qty_owned': 1, 'price': '1400.00', 'tracking': TRACKING_SOUND, 'account': ACCOUNT_EVENT_EQUIPMENT,
        'purchase_line': ('INV0005601', 'd&b Audiotechnik D40 Amplifier'), 'notes': '',
    },
    {
        'code': 'DB-D80', 'name': 'd&b D80 amplifier', 'category': 'AMP', 'unit': 'DAY',
        'qty_owned': 2, 'price': '1500.00', 'tracking': TRACKING_SOUND, 'account': ACCOUNT_EVENT_EQUIPMENT,
        'purchase_line': ('5973', 'D&B D80 AMPLIFIER - NL4'), 'notes': '',
    },
    {
        'code': 'PIO-CDJ3000', 'name': 'Pioneer CDJ-3000 media player', 'category': 'DJ', 'unit': 'DAY',
        'qty_owned': 2, 'price': '1200.00', 'tracking': TRACKING_SOUND, 'account': ACCOUNT_EVENT_EQUIPMENT,
        'purchase_line': ('5978', 'PIONEER CDJ3000 MEDIA PLAYER'), 'notes': '',
    },
    {
        'code': 'PIO-DJMV10', 'name': 'Pioneer DJM-V10 mixer', 'category': 'DJ', 'unit': 'DAY',
        'qty_owned': 1, 'price': '1600.00', 'tracking': TRACKING_SOUND, 'account': ACCOUNT_EVENT_EQUIPMENT,
        'purchase_line': ('7035', 'PIONEER DJMV10 MIXER'), 'notes': '',
    },
    {
        'code': 'EPSON-PU2220B', 'name': 'Epson EB-PU2220B 20k lumen laser projector', 'category': 'PROJECTOR',
        'unit': 'DAY', 'qty_owned': 1, 'price': '14000.00', 'tracking': TRACKING_PROJ, 'account': ACCOUNT_ELECTRICAL,
        'purchase_line': ('INV-24224', 'Epson 4K 3LCD Laser Projector, 20000 lumens - EBPU2220B'), 'notes': '',
    },
    {
        'code': 'EPSON-ELPLW08', 'name': 'Epson ELPLW08 wide-throw lens', 'category': 'LENS', 'unit': 'DAY',
        'qty_owned': 1, 'price': '2850.00', 'tracking': TRACKING_PROJ, 'account': ACCOUNT_EVENT_EQUIPMENT,
        'purchase_line': ('157261', 'LENS - ELPLW08 - WIDE THROW'), 'notes': '',
    },
    {
        'code': 'TOTEM-25', 'name': '2.5m Prolyte totem stand', 'category': 'STAGING', 'unit': 'DAY',
        'qty_owned': 0, 'price': '825.00', 'tracking': TRACKING_STAGE, 'account': '',
        'purchase_line': None,
        'notes': 'qty_owned not confirmed by MC — 0 is a placeholder, not a count.',
    },
]

# AURRAS standing 20% trade discount on the sound / DJ gear, valid from the same rate-card date.
TRADE_NOTE = 'standing 20% trade discount'
TRADE_PRICES = [
    {'code': 'DB-V10P', 'price': '680.00'},
    {'code': 'DB-VGSUB', 'price': '680.00'},
    {'code': 'DB-D40', 'price': '1120.00'},
    {'code': 'PIO-CDJ3000', 'price': '960.00'},
    {'code': 'PIO-DJMV10', 'price': '1280.00'},
]
