# Klikk Journals — Excel add-in

Office.js task pane over the Klikk Financials general ledger. Static bundle,
served by Django from this directory at `/excel-addin/` (publicly
`https://console.8-bit.space/backend/excel-addin/`).

Read-only by design. There is no write path in this add-in — not to Postgres,
not to Xero.

## The endpoints it depends on REQUIRE AUTHENTICATION. Do not regress them.

These four are `IsAuthenticated` and must stay that way:

| Endpoint | Purpose |
|---|---|
| `GET /xero/data/journals/search/` | detail rows |
| `GET /xero/data/journals/filters/` | entity / account / supplier pickers |
| `GET /xero/data/journals/pivot/` | server-side cross-tab (cube view) |
| `GET /xero/data/journals/pivot/dimensions/` | dimension + measure catalogue |

`journals/search/` was `AllowAny` **and publicly routed** until 2026-08-19. In
that state all 271,764 Klikk journal lines — accounts, suppliers, amounts,
tracking, every entity in the group — were retrievable from the open internet
with a single unauthenticated `curl`. It was locked down deliberately. If a
future change sets any of these back to `AllowAny`, or removes
`IsAuthenticated` from `DEFAULT_PERMISSION_CLASSES` reasoning in a way that
re-opens them, it re-creates a full general-ledger disclosure.

Anything touching `XeroJournalSearchView`, `XeroJournalFilterOptionsView`
(`apps/xero/xero_data/views.py`) or `pivot_views.py` should confirm afterwards:

```bash
# must print 401 for every line
for u in "journals/search/?limit=1" "journals/filters/" \
         "journals/pivot/dimensions/" "journals/pivot/?rows=account_type&measure=amount"; do
  curl -sS -o /dev/null -w "%{http_code} $u\n" \
    "https://console.8-bit.space/backend/xero/data/$u"
done
```

## Credentials

The add-in authenticates with a **DRF authtoken** (`Authorization: Token <key>`)
bound to a dedicated, non-staff, non-superuser Django user `excel-addin`. The
token is entered once by the operator and kept in the task pane's
`localStorage`, scoped to this add-in's origin on that machine.

Do **not** move it to `Office.context.document.settings` — that persists inside
the workbook, so the credential would travel to anyone the file is shared with.
And not `Office.context.roamingSettings` either: that is part of the
Outlook/Mailbox API and is `undefined` in Excel. Reading it threw on the first
line of startup and left the pane stuck on "Loading…" — the pane now carries a
boot watchdog that names the failing step rather than hanging silently.

It deliberately does **not** use the shared `KLIKK_API_TOKEN` service token
(`klikk_business_intelligence.permissions.ServiceTokenAuthentication` /
`HasServiceToken`). That secret is write-capable — it gates the three pricelist
write endpoints — and it is shared, so it could not be revoked for Excel alone.
A read-only client whose credential lives on a laptop gets its own least-
privilege identity. Keep it that way unless the whole token scheme is
consolidated deliberately.

Revoke by deleting the `excel-addin` row from `authtoken_token`.

## Data contract notes for anyone changing the API

Three properties of the journal mirror that silently corrupt totals if ignored:

1. `journal_type` mirrors every entry four ways (`journal` 142,437 /
   `transaction` 65,883 / `system_journal` 56,067 / `manual_journal` 7,377).
   Summing across all four double-counts.
2. `contact_id` is NULL on **every** `journal` row. Xero hangs the supplier off
   the source document, so the supplier is resolved via
   `transaction_source → contact` (`supplier_name`, with `supplier_via`
   recording which path was used). Don't "simplify" that back to `contact__name`.
3. The signed sum of `amount` over all rows is exactly `0.00` — a journal
   balances within itself. Grouping by a dimension that keeps both legs of an
   entry together (supplier alone, for instance) therefore returns zeros. The
   pivot endpoint detects this and returns `balancing_hint` rather than an
   unexplained empty result; keep that behaviour.

## Files

`manifest.xml` · `taskpane.html` · `app.js` · `styles.css` · `assets/icon-*.png`

Sideload on macOS by copying `manifest.xml` into
`~/Library/Containers/com.microsoft.Excel/Data/Documents/wef/`, then restart
Excel. Everything else is fetched over HTTPS, so updating the served files
updates every installed client — no re-sideload needed unless `manifest.xml`
itself changes.
