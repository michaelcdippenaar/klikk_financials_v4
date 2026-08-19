# Klikk Equipment Price List (`pricelist`)

*The hire rate card for Klikk's event gear: effective-dated prices, customer-specific rates, a quote calculator, and read-only links back to the asset in the Xero mirror.*
*Version 1 — 19 Aug 2026. Django app `apps/pricelist`, REST at `/api/pricelist/`, console page Pipeline → Pricing → Price List, six `pricelist_*` MCP tools.*

> **This feature never writes to Xero.** Not on a price change, not on a quote, not on a seed run. Every Xero field on an item (`xero_account_code`, `xero_tracking_option_id`, `xero_purchase_line`, `xero_fixed_asset_id`) is a **read-only reference into the local mirror tables** (`xero_metadata_*`, `xero_data_*`). The only tables this app writes are `pricelist_pricelistitem` and `pricelist_pricelistprice`.

---

## 1. What this is, and why

Klikk hires out event equipment — d&b V10P tops and V-GSUB subs driven by D40 / D80 amplifiers, a Pioneer CDJ-3000 / DJM-V10 DJ rig, an Epson EB-PU2220B 20 000-lumen projector with an ELPLW08 wide-throw lens, and 2.5 m Prolyte totem stands.

Until now the day rates lived in MC's head and in past invoices. This app makes them a table with three properties that matter:

1. **Prices are effective-dated.** A price is not "R 1 400.00"; it is "R 1 400.00 from 3 December 2025 until further notice". When the rate changes, the old row is closed rather than overwritten, so a quote can be re-priced as at any past date and last year's invoice can be explained.
2. **Customers can have their own rate.** Aurras Group has a standing 20 % trade discount on the sound and DJ gear. That is data, not a mental note.
3. **Each item points back at the asset in Xero** — the account it was capitalised to, the Profit Center it earns under, and the supplier bill line it was bought on. That link is for provenance and for the year-end conversation about which asset earns what; it is a pointer, never a write.

All money in this app is **ex VAT, ZAR**, stored as `Decimal(12,2)` and serialised as a **2-decimal string** (`"1400.00"`) so JSON never turns cents into a float.

---

## 2. Data model

Two tables. Nothing is ever deleted in normal use — prices are closed, items are deactivated.

### `pricelist_pricelistitem` — one hire-able asset / SKU

| Field | Type | Meaning |
|---|---|---|
| `code` | varchar(32), unique, indexed | Public identifier, e.g. `DB-D40`. Upper-cased and stripped on every save, so `db-d40` and ` DB-D40 ` are the same row. |
| `name` | varchar(200) | Display name, e.g. `d&b D40 amplifier`. |
| `category` | varchar(16), indexed | One of `PA`, `AMP`, `DJ`, `PROJECTOR`, `LENS`, `LIGHTING`, `STAGING`, `RIGGING`, `CABLING`, `OTHER`. Default `OTHER`. |
| `unit` | varchar(16) | One of `DAY`, `EVENT`, `WEEK`, `SEASON`. Default `DAY`. The whole seeded card is `DAY`. |
| `qty_owned` | positive integer | How many Klikk owns. Default `0`. |
| `description` | text | Longer spec. Searched by `?q=`. |
| `active` | boolean, indexed | `false` retires an item without deleting it. A quote line on an inactive item still prices, with a warning. |
| `xero_account_code` | varchar(20) | The Xero account the asset was capitalised to, e.g. `EE-SE01`. A string, not an FK. |
| `xero_tracking_option_id` | varchar(64) | `xero_metadata_xerotracking.option_id` of the Profit Center this asset earns under. |
| `xero_purchase_line` | FK → `xero_data_xeroinvoicelineitem`, `SET_NULL` | The supplier-bill line the asset was bought on. |
| `xero_fixed_asset_id` | UUID, nullable | Xero Fixed Assets register `AssetId`. **Never populated — see §9.** |
| `notes` | text | Free text, e.g. the TOTEM-25 placeholder warning. |
| `created_at` / `updated_at` | timestamp | Auto. |

Both Xero FKs are `on_delete=SET_NULL` on purpose: a Xero re-sync that rebuilds the mirror tables must not cascade-delete the rate card.

### `pricelist_pricelistprice` — one effective-dated price row

| Field | Type | Meaning |
|---|---|---|
| `item` | FK → `pricelist_pricelistitem`, `CASCADE` | The item this price is for. |
| `price` | numeric(12,2) | Ex VAT, ZAR. |
| `valid_from` | date, indexed | First day this price applies. |
| `valid_to` | date, nullable | **Inclusive** last day. `NULL` = still current. |
| `price_type` | varchar(16), indexed | `LIST`, `TRADE` or `SPECIAL`. Default `LIST`. |
| `customer` | FK → `xero_metadata_xerocontacts`, `SET_NULL`, nullable | `NULL` = applies to everyone. |
| `note` | text | Why this price exists, e.g. `standing 20% trade discount`. |
| `set_by` | varchar(64) | `mc` (console and seed), `claude-mcp` (MCP tools), or a username. |
| `created_at` | timestamp | Auto. |

### Lanes

A **lane** is the triple `(item, price_type, customer)`. Within a lane, rows are contiguous and non-overlapping: each row runs from its `valid_from` to the day before the next row's `valid_from`, and the newest row has `valid_to IS NULL`, meaning "current". `DB-D40` therefore has two independent lanes after the seed: `(DB-D40, LIST, NULL)` at R 1 400.00 and `(DB-D40, TRADE, Aurras)` at R 1 120.00. Both are open; they do not conflict, because a customer lane is an override, not a competitor.

Lane discipline is enforced in one place — `services.add_price` — and backed by a database constraint (below). Editing rows in the Django admin bypasses `add_price` and therefore does **not** auto-close the previous row; the admin module says so itself. Prefer the API, the MCP tool or the seed command.

### Constraints

`pricelist_price_valid_range` (check): `valid_to IS NULL OR valid_to >= valid_from`. A row cannot end before it starts.

`pricelist_no_overlapping_list_price` (exclusion): for rows where `price_type = 'LIST' AND customer_id IS NULL`, no two rows for the same item may have overlapping `daterange(valid_from, valid_to, '[]')`.

- **What it forbids:** two general list prices in force for one item on the same day. That is the one thing that would make "what do we charge for a V10P?" unanswerable.
- **What it deliberately does not cover:** `TRADE` and `SPECIAL` rows, and any row with a customer. Those are overrides, and `services.get_price` resolves them by picking the newest match — a customer may legitimately have a TRADE row and a SPECIAL row overlapping.
- **Why `NULL` works:** in Postgres, `daterange(valid_from, NULL, '[]')` is unbounded above, which is exactly "still current". The open row therefore collides with any later-dated general LIST row until `add_price` closes it — the constraint is doing the work, not trusting the application.
- **Why `btree_gist`:** the constraint mixes a range operator (`&&` on the daterange) with an equality operator (`=` on the `item_id` bigint) in a single GiST index. Postgres cannot put `=` on a scalar into a GiST index without the `btree_gist` extension, so migration `0001_initial` runs `BtreeGistExtension()` before creating the tables.

### Price resolution order

`services.get_price(item, on_date, customer, price_type)` — restated from its docstring:

1. Consider only rows **valid on `on_date`**: `valid_from <= on_date AND (valid_to IS NULL OR valid_to >= on_date)`.
2. If a `customer` was given, take that customer's rows, newest first (`-valid_from`, then `-id`), and return the first. **Any** price type qualifies — a row attached to a customer is an explicit override regardless of the label on it.
3. Otherwise, or if the customer has no row: take rows with `customer IS NULL AND price_type = <requested type>`, newest first, return the first.
4. If still nothing **and** the requested type was not `LIST`: repeat step 3 with `price_type = 'LIST'`.
5. Otherwise: no price.

`fallback_to_list` (returned by `resolve_price` and by the `pricelist_get_price` MCP tool) is `true` exactly when step 4 fired: you asked for `TRADE` or `SPECIAL`, no such row existed, and the general `LIST` price is what you are being shown. It is `false` whenever the requested type was found directly, and `false` when a customer row was returned in step 2. Read it as: *"this is not a negotiated rate — it is the list price."*

`on_date` defaults to today (`timezone.localdate()`), `price_type` to `LIST`.

---

## 3. The seeded rate card

Effective date: **2025-12-03**. Every row carries `set_by='mc'` and `note='seed:2025-12-03 rate card (Xero INV-0955 / INV-1037 / INV-1038 / INV-0969)'`.

Source: the December 2025 Aurras invoices **INV-0955, INV-1037, INV-1038 and INV-0969**. The list rate is the undiscounted unit amount on those lines; the Aurras trade rate is the same line after the 20 % trade discount. Every value was verified against the live Xero mirror on 19 Aug 2026 — do not "correct" them from memory.

### Items (list price is ex VAT, per day)

| Code | Name | Category | Qty owned | List price | Xero Profit Center | Capitalisation account |
|---|---|---|---|---|---|---|
| `DB-V10P` | d&b V10P point-source top | PA | 2 | R 850.00 | Equipment Rental - Sound Equipment | `EE-SE01` |
| `DB-VGSUB` | d&b V-GSUB subwoofer | PA | 4 | R 850.00 | Equipment Rental - Sound Equipment | `EE-SE01` |
| `DB-D40` | d&b D40 amplifier | AMP | 1 | R 1 400.00 | Equipment Rental - Sound Equipment | `EE-SE01` |
| `DB-D80` | d&b D80 amplifier | AMP | 2 | R 1 500.00 | Equipment Rental - Sound Equipment | `EE-SE01` |
| `PIO-CDJ3000` | Pioneer CDJ-3000 media player | DJ | 2 | R 1 200.00 | Equipment Rental - Sound Equipment | `EE-SE01` |
| `PIO-DJMV10` | Pioneer DJM-V10 mixer | DJ | 1 | R 1 600.00 | Equipment Rental - Sound Equipment | `EE-SE01` |
| `EPSON-PU2220B` | Epson EB-PU2220B 20k lumen laser projector | PROJECTOR | 1 | R 14 000.00 | Equipment Rental - Projector | `722` |
| `EPSON-ELPLW08` | Epson ELPLW08 wide-throw lens | LENS | 1 | R 2 850.00 | Equipment Rental - Projector | `EE-SE01` |
| `TOTEM-25` | 2.5m Prolyte totem stand | STAGING | 0 | R 825.00 | Equipment Rental - Stage and Structures | *(none)* |

### Aurras trade rows

Customer `AURRAS GROUP (PTY) LTD`, contact id `1f4a93c8-b49a-46da-a7fc-25bafd5fb2b9`, `price_type='TRADE'`, same `valid_from` of 2025-12-03, note `standing 20% trade discount`.

| Code | List | Aurras trade |
|---|---|---|
| `DB-V10P` | R 850.00 | R 680.00 |
| `DB-VGSUB` | R 850.00 | R 680.00 |
| `DB-D40` | R 1 400.00 | R 1 120.00 |
| `PIO-CDJ3000` | R 1 200.00 | R 960.00 |
| `PIO-DJMV10` | R 1 600.00 | R 1 280.00 |

`DB-D80`, the Epson projector, the lens and the totem have **no** Aurras row — Aurras pays list for those.

### Provenance notes carried from `seed_data.py`

- **DJ gear sits under the sound Profit Center.** The CDJ-3000 and DJM-V10 carry `Equipment Rental - Sound Equipment`, because that is how the INV-0955 lines were tracked and because there is **no** `DJ Gear` Profit Center in the tenant. This is recorded as fact, not as a preference.
- **The Epson is coded to `722`.** The EB-PU2220B supplier bill line is coded `722` *Electrical Equipment - @ cost*, while every other asset bill line is `EE-SE01` *Event Equipment @ Cost*. The seed records what Xero says. Whether `722` should be reclassified is a year-end question for the accountant — not something this app decides, and not something it can change (it never writes to Xero).
- **`TOTEM-25` has `qty_owned = 0` as a placeholder, not a count.** MC has not confirmed how many totems there are. The item's `notes` field says so. Treat `0` as "unknown", and fix it with a `PATCH` or `pricelist_upsert_item` when the count is known.
- **Tracking option ids are not hard-coded.** The seed stores the option *name* and resolves it at run time via `XeroTracking.objects.filter(name='Profit Center', option=<name>)`, because option ids are per-tenant UUIDs that would change if the tracking category were ever rebuilt.

---

## 4. REST API

Base path `/api/pricelist/` (wired in `klikk_business_intelligence/urls.py`). All responses are JSON except the CSV export.

**Money:** every money value in every response is a **string with exactly 2 decimals**, **ex VAT**, **ZAR** — `"1400.00"`, never `1400.0`. Quantities and day counts are also strings on quote lines (`"2"`, `"1.5"`). VAT and discount rates are strings too (`"0.15"`, `"0"`).

**Dates:** `YYYY-MM-DD`. A malformed date is a 400 (`bad date '3 Dec 2025'; expected YYYY-MM-DD`), never a 500.

**Customer parameters** accept either a Xero `contacts_id` UUID or a contact name. Resolution order: exact `contacts_id`, exact name, case-insensitive name, then `name__icontains`. If the contains-match hits more than one contact the request fails with 400 listing up to ten candidates (`customer 'gro' is ambiguous — matches: …`).

### GET `/api/pricelist/items/`

List the rate card.

| Param | Meaning |
|---|---|
| `category` | Exact category, upper-cased, e.g. `PA`. |
| `active` | `1`/`0`/`true`/`false`/`yes`/`no`/`on`/`off`. Omitted = both active and inactive. |
| `q` | Case-insensitive contains over `code`, `name`, `description`. |
| `customer` | Adds `customer_price` / `customer_price_type` / `customer_id` / `customer_name` to every item. |
| `date` | Price-effective date. Default today. |

```bash
curl -s 'http://127.0.0.1:8001/api/pricelist/items/?category=AMP&active=1&customer=AURRAS%20GROUP%20(PTY)%20LTD'
```

```json
{
  "count": 2,
  "categories": ["AMP"],
  "customer": {"contacts_id": "1f4a93c8-b49a-46da-a7fc-25bafd5fb2b9", "name": "AURRAS GROUP (PTY) LTD"},
  "items": [
    {
      "code": "DB-D40",
      "name": "d&b D40 amplifier",
      "category": "AMP",
      "unit": "DAY",
      "qty_owned": 1,
      "description": "",
      "active": true,
      "xero_account_code": "EE-SE01",
      "xero_tracking_option_id": "<Profit Center option_id from the mirror>",
      "xero_purchase_line_id": 48213,
      "xero_purchase_line": {
        "id": 48213,
        "invoice_number": "INV0005601",
        "description": "d&b Audiotechnik D40 Amplifier",
        "line_amount": "<4-dp line amount from the mirror>",
        "account_code": "EE-SE01"
      },
      "xero_fixed_asset_id": null,
      "notes": "",
      "created_at": "2026-08-19T17:04:11.221084+02:00",
      "updated_at": "2026-08-19T17:04:11.221097+02:00",
      "current_price": "1400.00",
      "current_price_valid_from": "2025-12-03",
      "last_changed": "2025-12-03",
      "price_count": 2,
      "customer_price": "1120.00",
      "customer_price_type": "TRADE",
      "customer_id": "1f4a93c8-b49a-46da-a7fc-25bafd5fb2b9",
      "customer_name": "AURRAS GROUP (PTY) LTD"
    }
  ]
}
```

Ids and timestamps above are illustrative; the rest is the real seeded data. Note that `current_price` is always the **general LIST** price (`customer=None`), even when a customer is passed — the customer's rate is reported separately in `customer_price`. `categories` lists the distinct categories **of the returned items**, not of the whole card. `last_changed` is the newest `valid_from` across every lane on the item.

**Errors:** 400 on a bad `date`, a bad `active` boolean, or an ambiguous `customer`. An unrecognised customer string is not an error here — it resolves to `None` and you get list prices with `"customer": null`.

### POST `/api/pricelist/items/`

Upsert an item by code.

Body: `code` (required), `name` (required), and any of `category`, `unit`, `qty_owned`, `description`, `active`, `xero_account_code`, `xero_tracking_option_id`, `xero_purchase_line_id`, `xero_fixed_asset_id`, `notes`. Optionally `price` **and** `valid_from` together to open the item's LIST lane in the same request, with `note` and `set_by` applied to that price row. `replace=true` is required to update an existing code.

```bash
curl -s -X POST http://127.0.0.1:8001/api/pricelist/items/ \
  -H 'Content-Type: application/json' \
  -d '{"code":"DB-Y10P","name":"d&b Y10P point-source top","category":"PA","unit":"DAY",
       "qty_owned":2,"xero_account_code":"EE-SE01",
       "price":"950.00","valid_from":"2026-09-01","set_by":"mc","note":"new stock"}'
```

```json
{
  "created": true,
  "item": {"code": "DB-Y10P", "name": "d&b Y10P point-source top", "category": "PA", "unit": "DAY",
           "qty_owned": 2, "active": true, "current_price": "950.00",
           "current_price_valid_from": "2026-09-01", "last_changed": "2026-09-01", "price_count": 1},
  "price": {"id": 31, "price": "950.00", "valid_from": "2026-09-01", "valid_to": null,
            "price_type": "LIST", "customer_id": null, "customer_name": null,
            "note": "new stock", "set_by": "mc", "created_at": "2026-08-19T18:02:44.118210+02:00"}
}
```

(`item` is abbreviated here — it is the full `item_to_dict` shape shown above.)

| Status | When |
|---|---|
| 201 | Item created. |
| 200 | Existing item updated (`replace=true` was passed); `"created": false`. |
| 409 | `item DB-D40 already exists (pass replace=true to update it)`. |
| 400 | `code is required`, `name is required`, `price and valid_from must be given together`, a bad `category` / `unit` / `qty_owned` / `xero_purchase_line_id` / `xero_fixed_asset_id`, or any `add_price` rejection (e.g. back-dating). |

The item save and the opening price are one transaction: if the price is rejected, no half-created item is left behind.

### GET `/api/pricelist/items/<code>/`

One item. Params `date` and `customer`, same meaning as the list endpoint. Response is the single `item_to_dict` object shown above. 404 `{"detail": "unknown item DB-X99"}` if the code is not in the table. The code in the URL is stripped and upper-cased, so `/items/db-d40/` works.

### PATCH `/api/pricelist/items/<code>/` (PUT is an alias)

Partial update of item fields. Both verbs are partial — a true "replace everything" PUT would silently blank the Xero links.

```bash
curl -s -X PATCH http://127.0.0.1:8001/api/pricelist/items/TOTEM-25/ \
  -H 'Content-Type: application/json' -d '{"qty_owned": 6, "notes": "count confirmed by MC 2026-08-19"}'
```

Returns the updated `item_to_dict`.

| Status | When |
|---|---|
| 400 | `code cannot be changed (it is the public identifier)`. |
| 400 | `price cannot be set here — POST /items/DB-D40/prices/` (same for `valid_from` and `prices`). |
| 400 | Field validation, e.g. `category must be one of ['PA', 'AMP', …]`. |
| 404 | Unknown code. |

### GET `/api/pricelist/items/<code>/price/`

Resolve the price that applies. Params: `date` (default today), `customer`, `type` (`LIST` default, `TRADE`, `SPECIAL`).

```bash
curl -s 'http://127.0.0.1:8001/api/pricelist/items/DB-D40/price/?customer=AURRAS%20GROUP%20(PTY)%20LTD&type=TRADE&date=2026-08-19'
```

```json
{
  "code": "DB-D40",
  "name": "d&b D40 amplifier",
  "unit": "DAY",
  "date": "2026-08-19",
  "requested_price_type": "TRADE",
  "price": "1120.00",
  "price_type": "TRADE",
  "customer_id": "1f4a93c8-b49a-46da-a7fc-25bafd5fb2b9",
  "customer_name": "AURRAS GROUP (PTY) LTD",
  "valid_from": "2025-12-03",
  "valid_to": null,
  "note": "standing 20% trade discount",
  "set_by": "mc",
  "price_id": 12,
  "resolved": true,
  "fallback_to_list": false
}
```

Asking for a trade price that does not exist returns the list price with the fallback flag set — `GET /items/DB-D80/price/?type=TRADE` gives `"price": "1500.00"`, `"price_type": "LIST"`, `"requested_price_type": "TRADE"`, `"fallback_to_list": true`.

If nothing resolves at all, the endpoint still returns 200 with `"resolved": false`, `"price": null` and every price field null. **A missing price is not an HTTP error** — callers must check `resolved`.

**Errors:** 404 unknown code; 400 bad `date`; 400 `type must be one of ['LIST', 'TRADE', 'SPECIAL']`; 400 ambiguous customer; 400 `unknown customer 'Aurrras'`.

An unresolvable customer is a **400 on every endpoint**, reads included. It deliberately does not fall through to "no customer": answering a question about Aurras' rate with the list price, and looking identical to a real answer, is worse than an error.

### GET `/api/pricelist/items/<code>/prices/`

Full price history, newest first (`-valid_from`, `-id`), across every lane.

```json
{
  "code": "DB-D40",
  "count": 2,
  "prices": [
    {"id": 12, "price": "1120.00", "valid_from": "2025-12-03", "valid_to": null, "price_type": "TRADE",
     "customer_id": "1f4a93c8-b49a-46da-a7fc-25bafd5fb2b9", "customer_name": "AURRAS GROUP (PTY) LTD",
     "note": "standing 20% trade discount", "set_by": "mc", "created_at": "2026-08-19T17:04:11.402117+02:00"},
    {"id": 3, "price": "1400.00", "valid_from": "2025-12-03", "valid_to": null, "price_type": "LIST",
     "customer_id": null, "customer_name": null,
     "note": "seed:2025-12-03 rate card (Xero INV-0955 / INV-1037 / INV-1038 / INV-0969)",
     "set_by": "mc", "created_at": "2026-08-19T17:04:11.318902+02:00"}
  ]
}
```

Two open rows (`valid_to: null`) is normal — one per lane.

### POST `/api/pricelist/items/<code>/prices/`

Add an effective-dated price and close the previous open row in the same lane. This is the **only** way to change a price through the API.

Body: `price` (required), `valid_from` (required), `price_type` (default `LIST`), `customer`, `note`, `set_by`.

```bash
curl -s -X POST http://127.0.0.1:8001/api/pricelist/items/DB-D80/prices/ \
  -H 'Content-Type: application/json' \
  -d '{"price":"1650.00","valid_from":"2026-09-01","price_type":"LIST","note":"2026 rate review","set_by":"mc"}'
```

```json
{
  "price": {"id": 33, "price": "1650.00", "valid_from": "2026-09-01", "valid_to": null, "price_type": "LIST",
            "customer_id": null, "customer_name": null, "note": "2026 rate review", "set_by": "mc",
            "created_at": "2026-08-19T18:11:03.907712+02:00"},
  "closed_previous": {"id": 5, "price": "1500.00", "valid_from": "2025-12-03", "valid_to": "2026-08-31",
                      "price_type": "LIST", "customer_id": null, "customer_name": null,
                      "note": "seed:2025-12-03 rate card (Xero INV-0955 / INV-1037 / INV-1038 / INV-0969)",
                      "set_by": "mc", "created_at": "2026-08-19T17:04:11.331004+02:00"}
}
```

Always **201**, whether a row was inserted or an existing same-date row was updated in place. `closed_previous` is `null` when there was no open row to close (first price in the lane, or an in-place update).

| Status | When |
|---|---|
| 400 | `price and valid_from are required`. |
| 400 | `price_type must be one of ['LIST', 'TRADE', 'SPECIAL']`. |
| 400 | `unknown customer 'Aurrras'` — the string resolved to nothing. |
| 400 | `price must be a number, got 'R1650'` / `price must be >= 0`. |
| 400 | Back-dating: `DB-D80 LIST: a price dated 2026-09-01 already exists; cannot back-date a new price to 2026-06-01. Fix the dates by hand.` |
| 400 | `price rejected by the database: …` — the exclusion constraint fired (a hand-edited row `add_price` could not foresee). |
| 404 | Unknown item code. |

### POST `/api/pricelist/quote/`

Pure calculation. **Nothing is persisted** — no quote row in Klikk, and certainly nothing in Xero.

Body: `lines` (non-empty array of `{code, qty, days}`; `qty` and `days` default to 1 and must be >= 0), `customer`, `date`, `discount_pct` (0–100, default 0), `vat_rate` (fraction, default 0.15).

```bash
curl -s -X POST http://127.0.0.1:8001/api/pricelist/quote/ \
  -H 'Content-Type: application/json' \
  -d '{"customer":"AURRAS GROUP (PTY) LTD","date":"2026-08-19",
       "lines":[{"code":"DB-V10P","qty":2,"days":3},
                {"code":"DB-VGSUB","qty":4,"days":3},
                {"code":"DB-D40","qty":1,"days":3}]}'
```

```json
{
  "date": "2026-08-19",
  "customer_id": "1f4a93c8-b49a-46da-a7fc-25bafd5fb2b9",
  "customer_name": "AURRAS GROUP (PTY) LTD",
  "vat_rate": "0.15",
  "discount_pct": "0",
  "lines": [
    {"code": "DB-V10P", "name": "d&b V10P point-source top", "unit": "DAY", "category": "PA",
     "qty": "2", "days": "3", "unit_price": "680.00", "price_type": "TRADE", "priced": true,
     "line_total": "4080.00", "valid_from": "2025-12-03", "note": "standing 20% trade discount"},
    {"code": "DB-VGSUB", "name": "d&b V-GSUB subwoofer", "unit": "DAY", "category": "PA",
     "qty": "4", "days": "3", "unit_price": "680.00", "price_type": "TRADE", "priced": true,
     "line_total": "8160.00", "valid_from": "2025-12-03", "note": "standing 20% trade discount"},
    {"code": "DB-D40", "name": "d&b D40 amplifier", "unit": "DAY", "category": "AMP",
     "qty": "1", "days": "3", "unit_price": "1120.00", "price_type": "TRADE", "priced": true,
     "line_total": "3360.00", "valid_from": "2025-12-03", "note": "standing 20% trade discount"}
  ],
  "subtotal": "15600.00",
  "discount": "0.00",
  "ex_vat": "15600.00",
  "vat": "2340.00",
  "incl_vat": "17940.00",
  "warnings": []
}
```

The arithmetic, in order, each step quantised to cents with `ROUND_HALF_UP`:

```text
line_total = unit_price × qty × days
subtotal   = Σ line_total
discount   = subtotal × discount_pct / 100
ex_vat     = subtotal − discount
vat        = ex_vat × vat_rate
incl_vat   = ex_vat + vat
```

An unknown code or an unpriceable item does **not** abort the quote. That line comes back with `"priced": false`, `"unit_price": null`, `"line_total": "0.00"`, and a warning is appended — `unknown item code 'DB-X99'` or `TOTEM-25: no price valid on 2026-08-19`. **Unpriced lines contribute nothing to the totals**, so a quote with warnings understates the job. Fix the warnings before sending anything to a client. A line on an inactive item still prices, with the warning `DB-D40 is marked inactive`.

**Errors:** 400 `lines must be a non-empty list of {code, qty, days}`; 400 `each line must be an object {code, qty, days}`; 400 `unknown customer 'Aurrras'`; 400 `discount_pct must be between 0 and 100`; 400 `vat_rate must be >= 0`; 400 `qty for DB-D40 must be >= 0`.

### GET `/api/pricelist/export/`

CSV of the rate card. Params: `date`, `active`, `customer`. Returns `text/csv` as an attachment named `klikk-pricelist-<date>.csv`.

Columns: `code, name, category, unit, qty_owned, current_price_ex_vat, price_valid_from, customer_price, xero_account_code, xero_tracking_option_id, xero_purchase_line_id, active, notes`.

```bash
curl -s -OJ 'http://127.0.0.1:8001/api/pricelist/export/?active=1&customer=AURRAS%20GROUP%20(PTY)%20LTD'
```

The export honours `active`, `customer`, `category` and `q`, with the same semantics as `GET /items/` — so the CSV matches whatever the console has on screen when Export CSV is pressed. `customer_price` is blank when no customer is passed. Money in the CSV is the raw 2-dp value with no `R` and no thousands separator.

### Authentication

**These endpoints are not authenticated.** The app defines no `permission_classes`, so it inherits the project default in `REST_FRAMEWORK` — `rest_framework.permissions.AllowAny` — exactly like the existing `/audit/` endpoints. That includes the write endpoints: `POST /items/`, `PATCH /items/<code>/` and `POST /items/<code>/prices/`.

This was left consistent with the existing apps rather than locked down unilaterally, because the MCP container currently runs **without** a `KLIKK_API_TOKEN` (`Dockerfile.mcp` sets only `KLIKK_API_BASE_URL`; `server.mjs` sends an `Authorization: Bearer` header only when `KLIKK_API_TOKEN` is set). Requiring authentication on the pricelist writes alone would break the MCP tools on the next deploy.

**Open item for MC:** decide whether the backend API should require authentication generally. If yes, that is a project-wide change (pricelist, audit, and the rest), plus issuing a token to the MCP container. It is not a pricelist decision.

---

## 5. MCP tools

Defined in the portal repo, `mcp/stock-market/server.mjs` (the `klikk-financials` MCP server). All six call the REST API above; none touch Xero.

| Tool | Kind | Arguments | Returns |
|---|---|---|---|
| `pricelist_list_items` | read | `category`, `active_only` (default `true`), `q`, `customer`, `date` | The full item list with `current_price`, `customer_price`, categories, plus an `agent_brief` that flags how many items have no current price. |
| `pricelist_get_price` | read | `code` (required), `date`, `customer`, `type` | One resolved price with `price_type`, validity window, `note`, `set_by` and `fallback_to_list`, plus a one-line brief. |
| `pricelist_price_history` | read | `code` (required), `limit` (1–200, default 50) | Newest-first price rows across all lanes, with `truncated` and `total_count`. |
| `pricelist_build_quote` | read (pure calc) | `lines` (required, `[{code, qty, days}]`), `customer`, `date`, `discount_pct`, `vat_rate` | The quote object from `POST /quote/` with `persisted: false` and a brief that repeats the warnings and lists unpriced lines. |
| `pricelist_set_price` | **mutating** | `code`, `price`, `valid_from`, `confirm` (all required), `price_type`, `customer`, `note` | The new row and `closed_previous`. |
| `pricelist_upsert_item` | **mutating** | `code`, `name`, `confirm` (all required), `category`, `unit`, `qty_owned`, `description`, `active`, `xero_account_code`, `xero_tracking_option_id`, `xero_purchase_line_id`, `notes`, `replace` | The created/updated item and whether it was created. |

**Both mutating tools refuse to run without `confirm=true`** and stamp every row they write with `set_by='claude-mcp'`, so the history shows which changes came from a Claude session. `pricelist_upsert_item` turns a 409 from the API into a clear instruction to pass `replace=true`.

The client-side guards are slightly stricter than the API: `pricelist_build_quote` requires `qty` and `days` to be **greater than zero** (the API allows 0), and `vat_rate` must be between 0 and 1.

### Phrasings that should trigger each tool

| MC says | Tool | Call |
|---|---|---|
| "show me the price list" / "what gear do we hire out?" | `pricelist_list_items` | `{}` |
| "what do the d&b amps go for?" | `pricelist_list_items` | `{"category": "AMP"}` |
| "what does Aurras pay?" | `pricelist_list_items` | `{"customer": "AURRAS GROUP (PTY) LTD"}` |
| "what do we charge for a V10P?" | `pricelist_get_price` | `{"code": "DB-V10P"}` → R 850.00 LIST |
| "what does Aurras pay for a V10P?" | `pricelist_get_price` | `{"code": "DB-V10P", "customer": "AURRAS GROUP (PTY) LTD"}` → R 680.00 TRADE |
| "do we have a trade rate on the D80?" | `pricelist_get_price` | `{"code": "DB-D80", "type": "TRADE"}` → R 1 500.00 with `fallback_to_list: true`, i.e. no, that is list |
| "what did we charge for the projector in January?" | `pricelist_get_price` | `{"code": "EPSON-PU2220B", "date": "2026-01-15"}` |
| "when did we last change the D80 price / who set it?" | `pricelist_price_history` | `{"code": "DB-D80"}` |
| "build me a quote for 2 tops, 4 subs and a D40 for a 3-day job for Aurras" | `pricelist_build_quote` | `{"customer": "AURRAS GROUP (PTY) LTD", "lines": [{"code": "DB-V10P", "qty": 2, "days": 3}, {"code": "DB-VGSUB", "qty": 4, "days": 3}, {"code": "DB-D40", "qty": 1, "days": 3}]}` → ex VAT R 15 600.00, incl VAT R 17 940.00 |
| "price the Epson and lens for a weekend, 10 % off" | `pricelist_build_quote` | `{"lines": [{"code": "EPSON-PU2220B", "days": 2}, {"code": "EPSON-ELPLW08", "days": 2}], "discount_pct": 10}` |
| "put the D80 up to R1 650 from 1 September" | `pricelist_set_price` | `{"code": "DB-D80", "price": 1650, "valid_from": "2026-09-01", "confirm": true}` |
| "give Aurras R1 050 on the D40 from October" | `pricelist_set_price` | `{"code": "DB-D40", "price": 1050, "valid_from": "2026-10-01", "price_type": "TRADE", "customer": "AURRAS GROUP (PTY) LTD", "confirm": true}` |
| "add the new Y10P tops to the price list" | `pricelist_upsert_item` | `{"code": "DB-Y10P", "name": "d&b Y10P point-source top", "category": "PA", "confirm": true}` then `pricelist_set_price` |
| "we now own 6 totems" | `pricelist_upsert_item` | `{"code": "TOTEM-25", "name": "2.5m Prolyte totem stand", "qty_owned": 6, "replace": true, "confirm": true}` |
| "retire the old CDJs" | `pricelist_upsert_item` | `{"code": "PIO-CDJ3000", "name": "Pioneer CDJ-3000 media player", "active": false, "replace": true, "confirm": true}` |

Ask `pricelist_price_history` before `pricelist_set_price` when the current rate is not certain — the write closes the open row, and undoing that means hand-editing dates.

---

## 6. Console page

> Money on the console is formatted `en-ZA`, matching the rest of the app (Financial Investments, Dividend Forecast): **`R 1 400,00`** — space thousands group, comma decimal mark. The CSV export and the API are unaffected; those carry the raw 2-dp value (`1400.00`).

**Pipeline → Pricing → Price List**, at `/app/pipeline/pricelist`. Source: `src/pages/Pricelist.vue`, API wrapper `src/api/pricelist.js`.

Two tabs, remembered in the URL as `?tab=`:

**Rate card** (`?tab=items`) — filters for category, free-text search (debounced 200 ms), customer and an "Active only" tick box. A badge shows the effective date (always today) and, when a customer is picked, "Trade: <name>". The table lists code, name, category, unit, qty, list price, last changed — plus a "Trade (Aurras)" column that appears only when a customer is selected, with a `TRADE`/`SPECIAL` pill next to the rate. Client-side pagination, 25/50/100 per page. Each row has two buttons:

- **History** — opens a dialog with the full price history for that item, newest first: validity range (`2025-12-03 → current` in accent colour for the open row), price, type pill, customer (`Everyone` when null), who set it, and the note.
- **Set price** — opens the set-price dialog, pre-filled with the current list price, today's date, type `LIST`, and whichever customer the filter is on. Fields: price (ex VAT, ZAR), valid from, price type, customer, note. Picking `TRADE` or `SPECIAL` with no customer raises an inline warning that the price would then apply to everyone. Saving posts to `POST /items/<code>/prices/` with `set_by='mc'`, then shows a success banner naming the new price and, if one was closed, the previous row and its new `valid_to`.

**Quote builder** (`?tab=quote`) — customer, date, discount %, VAT rate (0.15 = 15 %), and a repeating line editor (item dropdown fed by all active items, qty, days, Remove). "Price it" calls `POST /quote/` and renders the line table plus a Subtotal / Discount / Ex VAT / VAT / Incl VAT block. Warnings appear as an amber alert above the table, and unpriced lines are labelled "not priced". Nothing is saved.

Actions:

- **Export CSV** (page header) — downloads the API's `klikk-pricelist-<date>.csv`, honouring the current customer and "Active only" (not the category or search filter).
- **Copy** (quote result) — puts a plain-text quote on the clipboard: a header line with the customer and date, one line per item (`DB-V10P  d&b V10P point-source top  x2 x 3d  @ R 680.00  = R 4 080.00`), then the totals and any warnings. This is the thing to paste into WhatsApp or an email.
- **Download CSV** (quote result) — a client-side CSV of the quote lines and totals, named `klikk_quote_<date>.csv`.

The customer dropdown is currently hard-coded to two options — "List price (no customer)" and "AURRAS GROUP (PTY) LTD" — because Aurras is the only trade customer in v1. A searchable picker over Xero contacts is a follow-up; the API already accepts any contact id or name.

---

## 7. How to add or adjust a price

Three routes to the same code path (`services.add_price`). Pick whichever is in front of you.

### (a) Console

1. Pipeline → Pricing → Price List.
2. Find the item (search box, or filter by category).
3. **Set price**.
4. Enter the new price ex VAT, set **Valid from** to the first day the new rate applies, leave type `LIST` for a general change or pick `TRADE`/`SPECIAL` **and a customer** for a negotiated rate, add a note saying why.
5. **Save price**. The banner tells you what was closed.

### (b) MCP tool (in a Claude session)

```text
Put the D80 up to R1 650 from 1 September.
```

```json
{"code": "DB-D80", "price": 1650, "valid_from": "2026-09-01",
 "note": "2026 rate review", "confirm": true}
```

`confirm=true` is mandatory; the row lands with `set_by='claude-mcp'`.

### (c) curl

```bash
curl -s -X POST http://127.0.0.1:8001/api/pricelist/items/DB-D80/prices/ \
  -H 'Content-Type: application/json' \
  -d '{"price":"1650.00","valid_from":"2026-09-01","price_type":"LIST","note":"2026 rate review","set_by":"mc"}'
```

A customer rate:

```bash
curl -s -X POST http://127.0.0.1:8001/api/pricelist/items/DB-D40/prices/ \
  -H 'Content-Type: application/json' \
  -d '{"price":"1050.00","valid_from":"2026-10-01","price_type":"TRADE",
       "customer":"AURRAS GROUP (PTY) LTD","note":"renegotiated Sept 2026","set_by":"mc"}'
```

### What happens to the previous row

Inside one transaction, in the same lane `(item, price_type, customer)`:

- The open row (`valid_to IS NULL` with `valid_from` earlier than the new date) gets `valid_to = new valid_from − 1 day`. Setting a new D80 list price from 2026-09-01 closes the R 1 500.00 row at 2026-08-31. Ranges stay contiguous with no gap and no overlap, which is what keeps the exclusion constraint happy and what lets you re-price an old job correctly.
- The new row is inserted with `valid_to = NULL`.
- If the constraint rejects anything, the close is rolled back too — you never end up with a closed row and no replacement.

### Re-setting the same date is idempotent

If a row in that lane already has exactly this `valid_from`, it is **updated in place** — price overwritten, `note` and `set_by` overwritten only if you supplied non-empty values — and `closed_previous` comes back `null`. Correcting a typo in a price you set this morning is therefore safe: set it again with the same `valid_from`. This is also why `seed_pricelist` can be re-run without piling up rows.

### Back-dating is refused

If the lane's newest row has a `valid_from` **later** than the date you are asking for, the write is rejected with 400:

```text
DB-D80 LIST: a price dated 2026-09-01 already exists; cannot back-date a new price to 2026-06-01. Fix the dates by hand.
```

The code will not guess how to re-slice history. **What to do instead:**

- If you actually meant "correct the row that starts on 2026-09-01", re-post with `valid_from=2026-09-01` — that updates it in place.
- If you genuinely need to insert a rate into a closed period, the dates must be fixed by hand in the database or the Django admin (`/admin/pricelist/pricelistprice/`), adjusting the surrounding rows' `valid_to` so the lane stays contiguous and the general LIST lane never overlaps. Do that deliberately, ideally with someone watching, and add a `note` explaining the surgery.
- Remember the admin does **not** run `add_price`, so it will not close anything for you — the exclusion constraint is your only safety net there, and it only guards the general LIST lane.

### Writing into an already-covered period is refused

Separately from back-dating, a new price whose `valid_from` falls **inside an existing closed period** in the same lane is rejected with 400:

```text
DB-D40 TRADE/1f4a93c8-…: 2026-04-15 falls inside the existing period 2026-01-01..2026-06-30. Close or correct that row first.
```

This matters most in the `TRADE` / `SPECIAL` / customer lanes, which the exclusion constraint deliberately does **not** cover — without this check two prices could be valid for the same customer on the same day, and `get_price` would silently pick one of them.

### Prices have a ceiling

`price` is `numeric(12,2)`, so anything at or above R 10 000 000 000 is rejected with 400 rather than being allowed to reach Postgres (which would raise a `DataError` and surface as a 500). `NaN`, `Infinity` and non-numeric strings are rejected the same way.

---

## 8. How the Xero asset link works

Three fields on `PriceListItem` tie a rate-card entry to the asset as it appears in Xero. All three are read-only pointers into the local mirror; nothing is ever pushed back.

| Field | Points at | Example |
|---|---|---|
| `xero_account_code` | The account the asset was capitalised to, as a plain string | `EE-SE01` (Event Equipment @ Cost); `722` (Electrical Equipment - @ cost) for the Epson |
| `xero_tracking_option_id` | `xero_metadata_xerotracking.option_id` for the Profit Center the asset earns under | the option id of `Equipment Rental - Sound Equipment` |
| `xero_purchase_line` | FK to `xero_data_xeroinvoicelineitem` — the supplier-bill line the asset was bought on | `DB-D40` → Aurras bill `INV0005601`, line `d&b Audiotechnik D40 Amplifier` |

The seeded purchase-line links:

| Code | Supplier bill | Line description |
|---|---|---|
| `DB-VGSUB` | `5973` | `D&B V-GSUB SUBWOOFER - NL4` |
| `DB-D80` | `5973` | `D&B D80 AMPLIFIER - NL4` |
| `DB-D40` | `INV0005601` | `d&b Audiotechnik D40 Amplifier` |
| `PIO-CDJ3000` | `5978` | `PIONEER CDJ3000 MEDIA PLAYER` |
| `PIO-DJMV10` | `7035` | `PIONEER DJMV10 MIXER` |
| `EPSON-PU2220B` | `INV-24224` | `Epson 4K 3LCD Laser Projector, 20000 lumens - EBPU2220B` |
| `EPSON-ELPLW08` | `157261` | `LENS - ELPLW08 - WIDE THROW` |
| `DB-V10P`, `TOTEM-25` | — | no purchase line identified |

**These are resolved by lookup, not by hardcoded row id.** The seed command searches `xero_data_xeroinvoicelineitem` for the line whose invoice has that `invoice_number` and whose `description` matches exactly; if the exact match misses (descriptions come back from a re-sync with whitespace or casing tweaks) it falls back to a case-insensitive match on the first 40 characters. Tracking options are resolved the same way, by option *name* under the `Profit Center` category. That is why the seed is safe to run in any environment: on a fresh test database with an empty mirror the lookups simply miss, the command prints a warning, the field is left blank, and the rate card still seeds. A re-run never blanks a link that resolved before, and never overwrites a link MC attached by hand — the seed only assigns links it actually resolved.

When an item is serialised, the purchase line is expanded into `xero_purchase_line` with the invoice number, description, line amount and account code, so "where did this thing come from?" is answerable from the API alone.

---

## 9. The fixed-asset TODO

`PriceListItem.xero_fixed_asset_id` (UUID, nullable) exists on the model and in the migration. **It is never populated.** The seed does not set it, the console does not expose it, and the MCP tool does not accept it. The API will accept a UUID via `POST`/`PATCH` if you have one, but there is nothing to get one from.

Why it is empty:

1. **No Xero scope.** `apps/xero/xero_auth/views.py` defines `DEFAULT_SCOPES` as `openid`, `profile`, `email`, `offline_access`, `accounting.transactions`, `accounting.transactions.read`, `accounting.reports.read`, `accounting.journals.read`, `accounting.settings`, `accounting.settings.read`, `accounting.contacts`, `accounting.contacts.read`, `accounting.attachments`, `accounting.attachments.read`. **`assets.read` is not among them**, so the tenant token cannot read the Xero Fixed Assets register at all.
2. **No local mirror.** There is no fixed-asset table in the Xero mirror apps, so even with the scope there would be nowhere to look the asset up and nothing to point an FK at.

**The scope was deliberately not added as part of this work.** Adding a scope changes the OAuth consent for the whole Klikk Xero connection and requires MC to re-consent; that is not a side effect a price-list feature should have.

To populate it later:

1. Add `assets.read` to `DEFAULT_SCOPES`, then disconnect and re-consent the Xero app so the new token carries the scope. Verify the connection afterwards.
2. Add a mirror table and sync for the Fixed Assets register (asset id, name, number, purchase date, purchase price, account, status), on the same pattern as the other `xero_data` mirrors, and respect the Xero API budget rules for the sync.
3. Backfill `xero_fixed_asset_id` by matching each rate-card item on its purchase invoice (`xero_purchase_line.invoice`) and asset name, then eyeball the matches — nine items is a five-minute manual check, and a wrong asset link is worse than a blank one.

Until then, `xero_purchase_line` is the provenance link, and `"xero_fixed_asset_id": null` in every API response is expected, not a bug.

---

## 10. Commands

Run from the backend project root, with the virtualenv active.

### Seed / re-seed the rate card

```bash
python manage.py seed_pricelist
python manage.py seed_pricelist --only DB-V10P,DB-D80
python manage.py seed_pricelist --dry-run
```

Upserts the nine items and their 2025-12-03 LIST prices, then the five Aurras TRADE rows. The whole run is one transaction; `--dry-run` still performs every lookup and every `add_price` validation against real data and then rolls back, so it is a genuine rehearsal rather than a guess. `--only` takes a comma-separated list of codes and filters both the items and the trade rows.

Output ends with a summary line: items created/updated, prices created/updated/closed, trade rows skipped, tracking options resolved/missed, purchase lines resolved/missed.

**A re-run is idempotent. It does:**

- update `name`, `category`, `unit`, `qty_owned`, `xero_account_code` and `notes` back to the seed values;
- re-attach the tracking option and purchase line **if** they resolve;
- update the 2025-12-03 price rows in place (same `valid_from` → in-place update, no new row).

**It does not:**

- reactivate an item MC has deactivated — `active` is set on create only;
- blank a tracking option or purchase line that did not resolve on this run, or one MC attached by hand;
- touch any price row with a `valid_from` other than 2025-12-03. **A rate change made after the seed date survives a re-seed**, because the 2025-12-03 row is simply updated underneath it and the later row stays open. (Careful: this means a re-seed can silently restore the old seed price *underneath* a current price — the current price still wins, but the history now says something slightly different. Check with `pricelist_price_history` after a re-seed if prices have moved on.)

If the Aurras contact is not in `xero_metadata_xerocontacts`, the trade rows are skipped with a warning rather than failing.

### Tests

```bash
python manage.py test apps.pricelist
```

---

## Where to look next

| Thing | File |
|---|---|
| Models and constraints | `apps/pricelist/models.py` |
| Resolution, effective-dating, quote maths | `apps/pricelist/services.py` |
| Endpoints, params, status codes | `apps/pricelist/views.py`, `apps/pricelist/urls.py` |
| The rate card and its provenance | `apps/pricelist/seed_data.py` |
| Seed behaviour and lookups | `apps/pricelist/management/commands/seed_pricelist.py` |
| `btree_gist` and the exclusion constraint | `apps/pricelist/migrations/0001_initial.py` |
| MCP tools | portal `mcp/stock-market/server.mjs`, documented in `mcp/stock-market/README.md` |
| Console page | portal `src/pages/Pricelist.vue`, `src/api/pricelist.js` |
