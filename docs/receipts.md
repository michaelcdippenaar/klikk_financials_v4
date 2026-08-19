# Klikk Financials — Audit → Receipts (Slippies review workflow)
*Living document. Describes the `apps.receipts` app (backend) and the console page `Audit → Receipts` (portal) as of branch `feature/receipts`, backend commit `1460497`. Written from the code, not from intent — where the code and this page disagree, the code wins and this page needs fixing.*
*Version 2 — 19 Aug 2026. Updated for the auth gate (§3, §7) and the journal-type discriminator (§5); deployed, see §10.*

---

## 0. What it is

A review workflow over the WhatsApp Slippies receipt register so MC and the bookkeeper can triage which receipts still need capturing into Xero. It lists every slip in `whatsapp.klikk_slips`, shows the receipt image/PDF next to its OCR fields and (where found) the matched Xero journal, and lets a reviewer flag a slip **to process** or record a **decision** that closes it out, with a note and a comment thread. The bookkeeper filters to the flagged slips, exports CSV/XLSX and captures those into Xero.

**Nothing in this feature writes to Xero.** It is a register and a worklist only. The only tables it writes are its own two review tables (§2). The register itself is never written.

---

## 1. Data source — `whatsapp.klikk_slips`

**Not a Django model.** The table is owned and maintained by the WhatsApp pipeline in the same compose stack (`klikk-financials-whatsapp-bridge` + `klikk-financials-whatsapp-sync`, daily 06:00 SAST). The backend reads it with **parameterised raw SQL via `django.db.connection`** only (`apps/receipts/services.py`). It is never `ALTER`ed, never written, and has no FK from the receipts tables.

Today: **464 rows, 147 with `synced_to_xero = false`**.

Columns (from `information_schema`, 19 Aug 2026):

| Column | Type | Used by receipts as |
|---|---|---|
| `sha256` | text | primary key / URL key / loose key into `SlipReview` & `SlipComment` |
| `slip_ts` | timestamptz | FY bucketing (§4), default ordering, `date` column in export |
| `filename` | text | display, `q` search (`ilike`) |
| `source` | text | display only (`chat_export` 326, `archive5_2026` 79, `archive4_2026` 39, `pack_2025_2026` 14, `mc_dropped`, `drive_tanja`, `live_sync`, `email_accounts_alias`) |
| `mime_ext` | text | `jpg` (457) / `pdf` (7) → `mime`, `is_pdf` |
| `byte_size` | bigint | display |
| `file_bytes` | bytea | **never selected** by list/detail/export — only the signed viewer (§7) reads it |
| `xero_status` | text | raw status; normalised to `status_group` (below) |
| `xero_detail` | text | free text from the recon, e.g. `journal_number=697` |
| `imported_at` | timestamptz | not used |
| `synced_to_xero` | boolean | `synced` filter; `synced` column in export |
| `ocr` | jsonb | supplier / total / category / slip_date / payment_method in list; full object + `items` in detail |
| `search_tsv` | tsvector | `q` filter (`plainto_tsquery('simple', …)`) |
| `journal_number` | integer | Xero journal join (§5); `journal_number` column in export |
| `xero_org` | text | scopes the journal join to a tenant (`Klikk (Pty) Ltd` 136, `Dippenaar Family` 3, blank 325) |

**`xero_status` → `status_group`** (`STATUS_GROUP_SQL`): `MATCHED%` → `MATCHED`, `PENDING%` → `PENDING`, `NOT IN XERO%` → `NOT IN XERO`, `skipped%` (case-insensitive) → `SKIPPED`, anything else → `upper(xero_status)`. Current raw values: `MATCHED` 178, `MATCHED (auto-recon 2026-08-17)` 139, `PENDING` 111, `NOT IN XERO` 34, `skipped (no/negative total)` 2.

**`ocr` jsonb keys** (key → rows having it, 19 Aug 2026): `raw_text` 463, `supplier` 462, `currency` 462, `category` 462, `total` 459, `items` 448, `slip_date` 446, `payment_method` 354, `vat` 250, `vat_no` 231, `illegible` 67, `notes` 13, `customer` 3, `description` 1, `not_a_receipt` 1. `items` is a list of `{description, amount}`. The receipts API surfaces `supplier`, `total`, `category`, `slip_date`, `payment_method` on every row, and the whole `ocr` object plus a normalised `items[]` on the detail endpoint only. Everything in `ocr` is a **string** (including `total`, `amount`, `vat`); `category` is free text from OCR (e.g. `Meals` 127, `Fuel` 104, `Hardware` 78, `Electronics` 26, but also `Meals / Entertainment`, `Repairs & Maintenance (Painting labour)`), so the `category` filter is an `ilike` on the literal value, not an enum.

### Two data hazards, both guarded in code

1. **`ocr->>'total'` is a string and is not always numeric.** 459 rows have a total; 2 are non-numeric for the regex (`-259.00`, `-579.00` — negative values, the two `skipped (no/negative total)` rows). Every numeric use goes through `TOTAL_SQL`:
   ```sql
   case when s.ocr->>'total' ~ '^[0-9]+(\.[0-9]+)?$' then (s.ocr->>'total')::numeric end
   ```
   Never a bare `::numeric` — one bad row would 500 the whole list. Non-matching rows get `total = null` (sorted `nulls last`, excluded from `sum_total`, excluded by `min_total`/`max_total`).
2. **`file_bytes` is never selected** by `SLIP_COLUMNS`, the detail query, or the export. The list of ~460 rows must stay cheap; blobs are only read one-at-a-time by `apps.audit.slip_view.slip_file_view` (§7).

---

## 2. The two managed tables (`receipts/0001_initial`)

Both keyed on `sha256` as a **loose key** — no FK to `klikk_slips`, because the register is not a Django model and must never be altered. Writes check `slip_exists(sha256)` first (404 otherwise).

**`receipts_slipreview`** — one row per slip, upserted (`update_or_create`).

| Column | Type | Notes |
|---|---|---|
| `sha256` | char(64) PK | |
| `to_process` | bool, default `false` | "needs capturing in Xero" flag |
| `decision` | char(20), default `''` | enum below |
| `note` | text, default `''` | free text for the bookkeeper / auditor |
| `updated_by` | char(150), default `''` | username of the JWT user at last write (empty if unauthenticated — can't happen via the API, writes are gated) |
| `updated_at` | timestamptz, `auto_now` | |

Decision enum (`DECISION_CHOICES`): `''` (Undecided), `CAPTURE` (Capture in Xero), `MEAL_SKIP` (Meal — skip), `PERSONAL`, `DUPLICATE`, `ALREADY_IN_XERO`. The API upper-cases input and rejects anything else with 400.

**`receipts_slipcomment`** — append-only thread. `id` bigserial, `sha256` char(64) indexed, `text`, `author` char(150), `created_at` (`auto_now_add`); ordered by `created_at`. There is no edit or delete endpoint.

The raw SQL never joins these tables. Review state is attached after the query by two ORM lookups per page (`attach_review_state`), and the `to_process` / `decision` filters are implemented as `s.sha256 = any(%s)` against a list of sha256s pulled from the ORM first.

---

## 3. Endpoints — mounted at `/audit/receipts/`

`klikk_business_intelligence/urls.py`: `path('audit/receipts/', include('apps.receipts.urls'))`, placed **before** `path('audit/', …)` so it isn't shadowed. Behind nginx the public prefix is `/backend`, so the production URL is `https://console.8-bit.space/backend/audit/receipts/`.

**Auth, stated plainly.** The project's DRF default is `AllowAny`, so every endpoint here opts out of it explicitly: **all five require an authenticated user and return 401 otherwise.** The four DRF views carry `@permission_classes([IsAuthenticated])`. The export is a plain Django view (§3.5) and therefore has no permission classes at all, so it carries `@drf_login_required` instead — a decorator in `views.py` that instantiates `DEFAULT_AUTHENTICATION_CLASSES`, runs them against the raw request, and on failure returns `401 {"detail": "Authentication credentials were not provided."}` with `WWW-Authenticate: Bearer realm="api"`. It is applied outside `@require_GET` so an anonymous caller never reaches the view body, and only ever on a GET, where session auth's CSRF enforcement is a no-op.

The console sends a simplejwt Bearer token on every call including the export (`rest_framework_simplejwt.authentication.JWTAuthentication` is first in `DEFAULT_AUTHENTICATION_CLASSES`; `TokenAuthentication` and `SessionAuthentication` are also enabled project-wide and satisfy the check too). The signed slip viewer (§7) is the one deliberate exception and stays public — a browser `<img>` / `<iframe>` cannot carry a Bearer token, and exported spreadsheets link to it; its guard is the HMAC in `?s=`.

### 3.1 `GET /audit/receipts/` — list — **auth required**

All filters are optional and combine with AND. Unparseable values are **ignored** (not 400) — a bad `fy` or `date_from` silently drops that filter.

| Param | Accepts | Effect |
|---|---|---|
| `q` | text | `search_tsv @@ plainto_tsquery('simple', q)` OR `filename ilike %q%` OR `ocr->>'supplier' ilike %q%` |
| `synced` | `1/true/yes/on` · `0/false/no/off` | `synced_to_xero = …` |
| `status` | `MATCHED` · `PENDING` · `NOT IN XERO` · `SKIPPED` (case-insensitive) | on `status_group` |
| `fy` | `FY26` / `FY2026` | `slip_ts` in Africa/Johannesburg between 2025-07-01 and 2026-06-30 (§4) |
| `date_from` / `date_to` | ISO date | inclusive, on the local date of `slip_ts` |
| `to_process` | bool | `true` = sha256 in `SlipReview(to_process=True)`; `false` = not in that set (includes slips with no review row) |
| `decision` | `NONE` / `UNDECIDED` · or an enum value | `NONE`/`UNDECIDED` = no review row OR `decision=''`; enum = exactly that decision. Unknown values are ignored. The console sends `decision=NONE` for "Undecided" |
| `category` | text | `ocr->>'category' ilike %s` (exact text, case-insensitive; no wildcard added) |
| `min_total` / `max_total` | decimal | on `TOTAL_SQL` |
| `ordering` | `slip_ts`, `-slip_ts` (default), `total`, `-total`, `supplier`, `-supplier`, `xero_status`, `-xero_status` | whitelist only; anything else → default |
| `page` | int ≥ 1, default 1 | |
| `page_size` | 1..200, default 50 | |

`totals` is computed over the **whole filter**, not the page.

Response (shape from `_shape_row` + `attach_review_state`; values from a real row):

```json
{
  "count": 147,
  "page": 1,
  "page_size": 50,
  "num_pages": 3,
  "totals": {"count": 147, "sum_total": "61234.50"},
  "results": [
    {
      "sha256": "a56aae47681598cc4d128d63b6ce82cb6e32215f1205ed4e0de3604c379f8a74",
      "slip_ts": "2026-03-06T09:50:33+00:00",
      "filename": "PHOTO-2026-03-06-11-50-33 3.jpg",
      "source": "archive5_2026",
      "mime_ext": "jpg",
      "mime": "image/jpeg",
      "is_pdf": false,
      "byte_size": 145388,
      "xero_status": "MATCHED (auto-recon 2026-08-17)",
      "status_group": "MATCHED",
      "xero_detail": "journal_number=697",
      "synced_to_xero": true,
      "journal_number": 697,
      "xero_org": "Klikk (Pty) Ltd",
      "fy": "FY26",
      "supplier": "The Office Crew - Stellenbosch",
      "total": "13.50",
      "category": "Stationery",
      "slip_date": "2026-03-06",
      "payment_method": "Card",
      "view_url": "https://console.8-bit.space/backend/audit/slip/a56aae47…/?s=<32-hex>",
      "journal": {
        "journal_number": 697,
        "date": "2026-03-06",
        "description": "…",
        "debit": "13.50",
        "credit": "13.50",
        "amount": "13.50",
        "account_code": "…",
        "account_name": "…",
        "contact_name": "…"
      },
      "review": {"to_process": false, "decision": "", "note": "", "updated_by": "", "updated_at": null},
      "comment_count": 0
    }
  ]
}
```

Notes on the shape: money fields are **strings** quantised to 2dp (`"13.50"`), `null` when absent; `journal` is `null` when no journal resolved (§5); `review` is always present (defaults when no `SlipReview` row exists); `fy` is derived server-side from `slip_ts` in SLIP_TZ. `sum_total` above is illustrative — it is `coalesce(sum(TOTAL_SQL), 0)` over the filter.

### 3.2 `GET /audit/receipts/<sha256>/` — detail — **auth required**

Same row as the list plus `ocr` (full jsonb object), `items` (`[{description, amount}]`, normalised from `ocr.items`; `[]` when absent) and `comments` (`[{id, text, author, created_at}]` oldest first). 404 `{"detail": "slip not found"}` for an unknown sha256.

### 3.3 `PATCH /audit/receipts/<sha256>/review/` — **auth required**

Body: any subset of `{to_process, decision, note}`; at least one must be present (else 400 `nothing to update …`). `to_process` is truthy on `true, 1, "1", "true", "True", "yes", "on"`; anything else is `false`. `decision` is upper-cased and must be in the enum (else 400 `decision must be one of […]`). `updated_by` is set from the request user on every write.

```
PATCH /audit/receipts/a56aae…/review/
Authorization: Bearer <jwt>
{"to_process": true, "note": "Office Crew print job — capture to 429 Printing & Stationery"}

200
{"to_process": true, "decision": "", "note": "Office Crew print job — capture to 429 Printing & Stationery",
 "updated_by": "mc", "updated_at": "2026-08-19T18:42:11.120334+00:00"}
```

### 3.4 `POST /audit/receipts/<sha256>/comments/` — **auth required**

Body `{"text": "…"}` (trimmed, required → 400 `text is required`). Returns 201 `{"id": 12, "text": "…", "author": "mc", "created_at": "…"}`.

### 3.5 `GET /audit/receipts/export/?…&format=csv|xlsx` — **auth required**

Same filter and `ordering` params as the list; **no pagination** — every matching row. Default `format=csv`; anything other than `csv`/`xlsx` → 400. Filename `receipts-YYYY-MM-DD.csv|xlsx` via `Content-Disposition: attachment`. XLSX uses `openpyxl` (in `requirements.txt`); if the import fails the view degrades to CSV and sets `X-Export-Note: openpyxl unavailable; degraded to csv`.

Columns, in order: `date` (= `slip_ts`), `supplier`, `total`, `category`, `xero_status`, `status_group`, `journal_number`, `synced`, `to_process`, `decision`, `note`, `filename`, `sha256`, **`view_url`**.

**Why it is a plain Django view.** `receipts_export_view` is `@require_GET`, not `@api_view`, on purpose: DRF content negotiation treats `?format=` as a renderer override and would 404 on `csv`/`xlsx` before the view ran. Consequence: it cannot use DRF permission classes, so auth is explicit — `@drf_login_required`, outermost (§3). Auth is checked **before** the `format` validation, so an anonymous caller gets the same 401 for `format=csv`, `format=xlsx` and `format=exe` and cannot probe the endpoint.

---

## 4. FY bucketing

Klikk FY runs **Jul–Jun and is named by the ending year**: FY26 = 2025-07-01 .. 2026-06-30 (`fy_range`), `fy_label(2026-08-04) = 'FY27'`. Both the `fy` filter and the `fy` field on each row bucket on `slip_ts` **cast to `Africa/Johannesburg`** (`SLIP_TZ`), i.e. `(s.slip_ts at time zone 'Africa/Johannesburg')::date`, not UTC — a slip photographed at 00:30 SAST on 1 July belongs to the new FY even though its UTC timestamp is still 30 June. `date_from`/`date_to` use the same local date.

`slip_ts` is nullable in the register: rows with a NULL `slip_ts` sort last under the default ordering, get `fy: null`, and are excluded by any `fy`/date filter. At the time of writing (19 Aug 2026) the production table has **0** NULL `slip_ts` rows (`count(*) filter (where slip_ts is null)` = 0); the code handles the case regardless.

The portal mirrors the rule in `src/utils/receipts.js` (`fyForDate`, `FY_START_MONTH = 7`) and offers `FY25`/`FY26`/`FY27` in the filter; the backend accepts any `FY\d\d` / `FY\d\d\d\d`.

---

## 5. The Xero journal join

`JOURNAL_LATERAL_SQL` attaches at most one `journal` object per slip:

`journal_number` **is not a key on its own.** It is unique only within `(organisation_id, journal_type)`, and it collides on both axes:

- **Across tenants.** Journal #697 exists in both `Klikk (Pty) Ltd` and `Dippenaar Family` (2 lines each, today).
- **Across journal types in the same tenant.** The mirror holds four `journal_type` values — `journal` (142,437 rows), `transaction` (65,859), `system_journal` (56,067), `manual_journal` (7,377) — and ~1,225 same-org `journal_number` values appear under more than one of them. Joining on number + org alone therefore aggregates two unrelated transactions into a single summary: `amount`/`debit`/`credit` become their sum and `description`/`account` come from whichever carried the larger debit. That was **BUG-4**, fixed here.

So the join is scoped on **three** things:

1. `j.journal_number = s.journal_number`
2. `j.journal_type = 'journal'` (`services.JOURNAL_TYPE`). The Slippies auto-recon writes `journal_number` from the Xero **Journals** feed. Verified against production 19 Aug 2026: of the 139 slips carrying a `journal_number`, **30 resolve under `journal`** — 28 of them tying to the slip's OCR total *to the cent* and to within 3 days of the slip date — **1 under `manual_journal`** (#216150: 8 lines, R1,000 against a R2,515 slip, dated 19 months earlier — a false match, now correctly dropped) and **0 under `transaction`** (transaction rows are invoice/bank-derived and live in a different number space; Klikk's `journal` numbers top out at 46,902 while `transaction` runs to 999,995).
3. `j.organisation_id` = the tenant named by `s.xero_org`, or `services.KLIKK_TENANT_ID` (`41ebfa0e-…`) when `xero_org` is blank/NULL. An `xero_org` that names *no* known tenant resolves to NULL and therefore to **no** journal — never to another tenant's lines. (Today all 325 blank-`xero_org` slips also have a NULL `journal_number`, so the Klikk default is a forward guard rather than live behaviour.)

**Aggregated to exactly one row per slip** (`left join lateral … group by j.journal_number`) so the list never fans out. The summary is built from the **debit side only** (`filter (where j.debit > 0)`): `amount = sum(debit) filter (debit > 0)`, and the "main" line (`description`, `account_code`, `account_name`, `contact_name`) is the first debit line ordered by `coalesce(a.type = 'BANK', false), j.debit desc, j.id` — the largest debit on a non-BANK account, i.e. the expense side of the receipt. A journal with no debit line at all falls back to all lines rather than returning nulls. `debit` and `credit` remain sums over **all** lines (the journal's own totals; credits are stored negative); `date = min(date)`.

**Two data caveats — both upstream, neither a query bug.**

- *Incomplete mirror.* 139 slips carry a `journal_number` but only 31 of those numbers exist in `xero_data_xerojournals` at all; after the type discriminator, **30 slips resolve**. The other ~109 matched slips show `journal: null` ("Unmatched" in the console's Xero panel) even though `xero_status` says MATCHED and `xero_detail` names the journal. 106 of them carry a number ≥ 50,000, which is outside Klikk's `journal` number range entirely — those came from somewhere other than the Journals feed. Fixing that is a Xero-sync / recon job, not a receipts change.
- *Stale matches survive the fix.* Two of the 30 resolve to a journal that is clearly not the receipt: #697 → a 2019 journal of R5,500 against a 2026 R13.50 Office Crew slip, and #29040 → a 2023 journal of R130,864 against a R224 slip. The type discriminator cannot catch these — the recon simply recorded a number that means something else. A date/amount plausibility guard on the join would catch them; it is **not** implemented, and it is a decision for MC rather than a silent code change, because it would also hide genuine matches whose journal date drifts from the slip date.

---

## 6. How "to process" flows to the bookkeeper

The reviewer (MC) opens **Audit → Receipts** in the console and narrows the list to what still matters — typically `Not synced` (`synced=false`), or `Pending` (`status=PENDING`), and a fiscal year. The header totals show how many slips and how many rand are in that cut. Each row can be flagged or given a decision inline; the **View** button opens the detail modal, which shows the receipt image (or the PDF in an iframe) on the left and, on the right, the OCR fields (supplier, total, slip date, payment method, category, line items), the Xero panel (status, detail, synced, org, and the journal if one resolved), the Review block and the comment thread.

Reading the receipt next to the OCR, the reviewer does one of two things per slip:

- **Flag it `to_process = true`** — it is a business expense that is not in Xero and needs capturing. Optionally set `decision = CAPTURE` and write a note saying where it should go ("capture to 429 Printing & Stationery", "split: R1,680 tip is personal").
- **Record a decision that closes it out** — `MEAL_SKIP` (meal under the policy pile, not captured), `PERSONAL` (not a Klikk expense), `DUPLICATE` (same receipt already in the register / already captured under another slip), or `ALREADY_IN_XERO` (captured but the recon didn't match it). The note carries the reasoning; anything that needs a back-and-forth goes in the comment thread, which records who said what and when.

The Review block saves `to_process` immediately on toggle; `decision` + `note` save together on **Save review**. Every save stamps `updated_by`/`updated_at`, so the list shows who last touched a slip.

The bookkeeper then filters **`to_process = true`** (Flagged to process), optionally by FY, and presses **Export CSV** or **Export XLSX**. The export contains every matching row, not just the page, and **carries a `view_url` column** — a signed, permanent link to the receipt image (§7) — so every spreadsheet row opens the original receipt in one click without needing console access. The bookkeeper captures those rows into Xero from the spreadsheet, with `supplier`, `total`, `category`, `note` and the image to hand.

Closing the loop is manual today: once a slip is captured, the reviewer either flips `to_process` off and sets `decision = ALREADY_IN_XERO`, or waits for the next Slippies recon run to set `synced_to_xero = true` / `xero_status = MATCHED` on the register row. The feature does not detect that itself. **Nothing here writes to Xero** — the register and the review tables are the only state; Xero capture happens in Xero, by the bookkeeper, under the usual Xero-write rule (MC's explicit instruction, logged in `audit.xero_writes` if done by Claude).

---

## 7. The signed viewer — `GET /audit/slip/<sha256>/?s=<hmac>`

`apps/audit/slip_view.py`, mounted in `apps/audit/urls.py` as `slip/<str:sha256>/`. `sig = HMAC-SHA256(SECRET_KEY, sha256).hexdigest()[:32]`, compared with `hmac.compare_digest`; wrong/missing → 403 `invalid signature`; unknown sha256 or NULL `file_bytes` → 404. On success it streams `file_bytes` with the mime guessed from `mime_ext`, `Content-Disposition: inline`, `Cache-Control: private, max-age=3600`. It is the **only** code path that reads `file_bytes`, and it reads one row at a time.

`slip_url(sha256)` builds `f"{base}/audit/slip/{sha256}/?s={sig}"`, with `base` from `SLIP_VIEW_BASE_URL` (`settings/base.py`: `os.environ.get('SLIP_VIEW_BASE_URL', 'https://console.8-bit.space/backend')`). Every list/detail/export row carries this as `view_url`.

**Trade-off, deliberate:** links are unguessable (32 hex chars of HMAC keyed on `SECRET_KEY`) but **never expire** — there is no timestamp in the signature — so a `view_url` pasted into a spreadsheet in August still works in the year-end audit. The cost is that a leaked link is a permanent read on that one receipt. Rotating `SECRET_KEY` invalidates every link ever issued.

### Security note — the reads are gated (done)

The reason all five receipts endpoints require auth (§3) is this section. Every list, detail and export row carries a `view_url`, so an open list endpoint would hand out a valid, non-expiring signed link for **every receipt in the register** to any anonymous caller — collapsing the unguessability the viewer relies on. An attacker would not need to guess the HMAC; they would ask the list for it. The list also exposes supplier, total and `payment_method` (sometimes carrying partial card numbers), and the detail endpoint exposes the full OCR `raw_text`.

The gate closes that: `receipts_list_view` / `receipt_detail_view` via `IsAuthenticated`, `receipts_export_view` via `@drf_login_required`. The console is unaffected — `src/api/receipts.js` goes through `apiClient`, which attaches the Bearer token and refreshes it on 401, on the export as well as the reads. The only thing that stopped working is an unauthenticated `curl`.

The viewer itself stays public **by design**: the modal renders it in a plain `<img>` / `<iframe>` and exported spreadsheets link to it, neither of which can send a token. Its guard is the HMAC. The residual exposure is therefore one receipt per leaked link, not the whole register.

**Still open, project-wide and outside this feature:** other backend read endpoints remain anonymous — `GET https://console.8-bit.space/backend/audit/checks/` returned 200 with no token on 19 Aug 2026. That is a separate piece of work.

---

## 8. Console page — Audit → Receipts

Portal worktree: route `audit/receipts` (name `audit-receipts`) in `src/router/routes.js`, nav entry under the **Audit** group in `src/layouts/PipelineLayout.vue` (Lucide `receipt`), page `src/pages/AuditReceipts.vue`, API client `src/api/receipts.js`, pure helpers `src/utils/receipts.js`.

- **Filters persisted in the query string** (`hydrateFromQuery` / `buildRouteQuery`): `q`, `fy`, `synced`, `status`, `to_process`, `decision`, `date_from`, `date_to`, `page`, `page_size` — defaults are omitted so a clean page has an empty query, and a filtered URL is shareable. `category`, `min_total`, `max_total`, `ordering` exist on the API but are not exposed as controls on the page today.
- **Server-side pagination**: `page` 1-based, `page_size` 25/50/100 (API max 200); the table never holds more than one page. `totals` in the header come from the API and cover the whole filter.
- Row actions: `to_process` toggle and `decision` select save inline via `PATCH …/review/`; **View** opens the detail modal; **Comment** opens it with the comment box focused. Export buttons call `/export/` with the current (non-paging) filters and download the file.
- All calls go through `apiClient`, which attaches the simplejwt Bearer token and refreshes on 401 — so, once the reads are gated (§7), the page keeps working unchanged.

---

## 9. Where it lives

| Piece | Path |
|---|---|
| Raw-SQL query layer, FY helpers, filter builder, shaping | `apps/receipts/services.py` |
| Views (list, detail, review, comments, export) | `apps/receipts/views.py` |
| `SlipReview`, `SlipComment`, `DECISION_CHOICES` | `apps/receipts/models.py` |
| URLs | `apps/receipts/urls.py` (`app_name = 'receipts'`), mounted from `klikk_business_intelligence/urls.py` |
| Migration | `apps/receipts/migrations/0001_initial.py` |
| Signed viewer + `slip_url()` | `apps/audit/slip_view.py`, `apps/audit/urls.py` |
| `SLIP_VIEW_BASE_URL` | `klikk_business_intelligence/settings/base.py` |
| App registration | `INSTALLED_APPS` → `'apps.receipts'` |
| Console | `klikk_portal`: `src/pages/AuditReceipts.vue`, `src/api/receipts.js`, `src/utils/receipts.js`, `src/router/routes.js`, `src/layouts/PipelineLayout.vue` |

---

## 10. Operational notes

- Migration `receipts/0001_initial` creates `receipts_slipreview` and `receipts_slipcomment` only; it does not touch `whatsapp.klikk_slips` or anything else. It was applied on deploy (19 Aug 2026) by the backend image's entrypoint.
- Deploy recipe: merge `feature/receipts` in both repos → `docker compose up -d --build klikk-financials` (the entrypoint runs `migrate`) → `docker compose up -d --build klikk-financials-console`.
- Post-deploy checks: `GET /backend/audit/receipts/` returns **401** anonymously; a signed `/backend/audit/slip/<sha>/?s=…` still returns **200** anonymously; the console serves the `audit/receipts` route.
- **Warning:** the backend image's ENTRYPOINT (`scripts/docker-entrypoint.sh`) runs `python manage.py migrate --noinput` on **every** container start against the live DB. Any `docker run` / `docker compose run` of that image without `--entrypoint python` (or similar override) will apply this migration as a side effect. Do not start the image casually from a worktree that contains the unapplied migration.
- `SLIP_VIEW_BASE_URL` must match the public prefix nginx serves the backend under (default `https://console.8-bit.space/backend`); if it is wrong every `view_url` in every export is dead.
- The feature depends on `openpyxl` for XLSX (already in `requirements.txt`); CSV needs nothing extra.
- Read §7 before deploying.
