# Klikk Journals — Excel add-in

Office.js task pane over the Klikk Financials general ledger. Static bundle,
served by Django from this directory at `/excel-addin/` (publicly
`https://console.8-bit.space/backend/excel-addin/`).

Read-only against Xero and against every other upstream system: there is no
write path to Xero in this add-in, and its credential cannot reach one either
(see [Credentials](#credentials)). It *does* write to our own Postgres — cube
comments and saved subsets/views — which is deliberate and is the only writing
it does anywhere.

## The endpoints it depends on REQUIRE AUTHENTICATION. Do not regress them.

These are `IsAuthenticated` and must stay that way:

| Endpoint | Purpose |
|---|---|
| `GET /xero/data/journals/search/` | detail rows |
| `GET /xero/data/journals/filters/` | entity / account / supplier pickers |
| `GET /xero/data/journals/pivot/` | server-side cross-tab (cube view) |
| `GET /xero/data/journals/pivot/dimensions/` | dimension + measure catalogue |
| `GET /xero/data/journals/pivot/members/` | member lists for the subset editor |
| `GET /xero/data/journals/pivot/drill/` | the detail behind one cell |
| `GET /xero/data/journals/pivot/subsets/` | saved member subsets |
| `GET /xero/data/journals/pivot/views/` | saved layouts |

All eight are pinned in `apps/user/test_auth_lockdown.py` (`GATED`), which
until 2026-09-03 covered only the first two.

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

## Comments on a cube cell

Right-click a cell on a cube sheet → New Comment, then **Sync comments**. The
comment is pinned to that intersection in `app.cube_comments` — measure, row
path, column path and the filter context that produced the number — so an agent
reading it later knows exactly which figure it refers to. `GET
.../pivot/comments/?status=open` is the agent's to-do queue; POST to
`.../comments/<id>/status/` marks one actioned or dismissed.

Filters are part of the anchor on purpose: the same row and column under a
different `journal_type` or date window is a different number, and a comment
about one must not silently reattach to the other. Re-posting the same cell
edits the existing comment rather than accumulating duplicates; an emptied
comment retracts it.

This writes to OUR Postgres only. Nothing here goes near Xero.

## Credentials

The add-in authenticates with a **DRF authtoken** (`Authorization: Token <key>`)
bound to a dedicated, non-staff, non-superuser Django user `excel-addin` whose
`role` is **`service_readonly`**. The token is entered once by the operator and
kept in the task pane's `localStorage`, scoped to this add-in's origin on that
machine.

The role is what makes the identity least-privilege. **Until 2026-09-03 it was
not.** The `excel-addin` user was `role=standard`, and `AuditorGateMiddleware`
narrowed only `role=auditor`, so this token passed every `IsAuthenticated` view
in the project. That included `XeroCreateDraftInvoiceView`
(`POST /xero/data/invoices/create-draft/`) — the one path in this codebase that
**writes to Xero** — and every Xero sync trigger (`update/journals/`,
`process/journals/`, `sync/documents/`, `aged-payables/sync/`,
`aged-receivables/sync/`, `quotes/sync/`, `invoices/sync/`), any of which can
spend the 5,000-call daily Xero API budget. The JavaScript never called those;
the credential could. Nothing was ever written to Xero through it.

`service_readonly` is enforced by the same middleware as the auditor role
(`apps/user/middleware.py`, gate C) — middleware and not a DRF permission
class, because most views set their own `permission_classes` and would silently
override a default. It is a pure allowlist:

| Allowed | |
|---|---|
| `GET`/`HEAD` on `/xero/data/journals/...` | the whole cube read surface — search, filters, pivot, dimensions, members, drill, subsets, views, comments |
| `GET` on `/xero/data/documents/<id>/file/` | the HMAC-signed receipt links a drill puts in its rows |
| `POST` `journals/pivot/comments/`, `.../comments/bulk/`, `.../comments/<id>/status/` | cube comments — **our Postgres only** |
| `POST` + `DELETE` `journals/pivot/subsets/`, `journals/pivot/views/` | saved subsets and views — our Postgres only |

Everything else answers **403**: every Xero write, every sync trigger, the rest
of `/xero/`, Investec, the pricelist, the audit surface, and the Django admin.
"Read-only" here means read-only **against Xero and against every other
system** — the add-in does write to our own Postgres, which is what cube
comments and saved views are.

Pinned by `apps/user/test_service_readonly_gate.py`. Note the gate is only real
because `resolve_request_user` understands `Authorization: Token <key>`; it
originally resolved sessions and `Bearer` JWTs alone, which is precisely the
credential shape this add-in does *not* use.

To apply the role to a service account:

```bash
ssh klikk-financials 'sudo docker exec klikk-financials-v4 python manage.py shell -c "
from django.contrib.auth import get_user_model
U = get_user_model()
u = U.objects.get(username=\"excel-addin\")
u.role = U.Role.SERVICE_READONLY
u.save(update_fields=[\"role\"])
print(u.username, u.role)
"'
```

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
consolidated deliberately — and note that a separate identity is only
least-privilege if it is also separately *scoped*, which is the lesson of the
2026-09-03 finding above: for two weeks it was separate and fully privileged.

If the add-in needs a new endpoint, widen the allowlist in
`apps/user/middleware.py` and add it to the test file. Do **not** move the
account back to `role=standard` to make a call work.

Revoke by deleting the `excel-addin` row from `authtoken_token`.

## Data contract notes for anyone changing the API

Three properties of the journal mirror that silently corrupt totals if ignored:

1. `journal_type` has ONE mirror, not four. `journal` (142,437 rows) is the
   legacy Xero Journals API feed, frozen at 2025-11-25 — Xero moved that API to
   the Advanced tier in March 2026 — and it re-states the same entries the live
   feeds carry, under its own journal numbers. The live feeds `transaction`
   (66,299), `system_journal` (56,067) and `manual_journal` (7,439) are
   COMPLEMENTARY: each covers source types the others do not, and together they
   are the whole ledger. No tenant even carries all four values — Klikk has no
   `system_journal`, Tremly has no `journal`.

   So summing `journal` alongside the live feeds double-counts (measured
   2026-09-03 against Xero's own Trial Balance: the mirror alone reproduced
   Xero's FY-to-date P&L exactly, 47/47 accounts for Dippenaar Family and 77/77
   for Klikk within the project's 0.05 tolerance, and adding it to the live
   feeds overstated the total by 2.01×). Summing the three live feeds together
   does not — that IS the reporting basis.

   Both `/journals/pivot/` and `/journals/search/` therefore exclude `journal`
   when no `journal_type` is given, matching the trial balance
   (`xero_cube/models.py`). Pass `journal_type=journal` to inspect the mirror
   deliberately; the search response's `mirror_excluded` flag says which mode
   you are in.

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

Served to Excel: `manifest.xml` · `taskpane.html` · `lib.js` · `app.js` ·
`styles.css` · `assets/icon-*.png`

Not served (the checks — see **Checks** at the end): `test/` · `package.json` ·
`eslint.config.mjs` · `Makefile` · `node_modules/`. The add-in directory is
published whole at `/excel-addin/`, so `_excel_addin_asset` in
`klikk_business_intelligence/urls.py` 404s those paths explicitly. Add a file
to that list if you add anything else to this folder that Excel does not fetch.

`lib.js` is the pure half of the pane — run merging, `dimf`/`rtotals`
serialisation, date serials, the Build-target rule — split out of `app.js`'s
single IIFE so node can require it with no DOM, no Office and no bundler.
**It must be served alongside `app.js`:** `app.js` reads `window.KlikkLib` at
load and, if it is missing, prints a boot panel saying so rather than dying on
`LIB is undefined`. `taskpane.html` loads it first.

Sideload on macOS by copying `manifest.xml` into
`~/Library/Containers/com.microsoft.Excel/Data/Documents/wef/`, then restart
Excel. Everything else is fetched over HTTPS, so updating the served files
updates every installed client — no re-sideload needed unless `manifest.xml`
itself changes.

**Bump `<Version>` on every manifest change.** Excel caches a sideloaded
manifest by `Id` + `Version`, at

```
~/Library/Containers/com.microsoft.Excel/Data/Library/Application Support/
  Microsoft/Office/16.0/Wef/{…}/…/Manifests/<id>_<version>
```

and that cached copy is the one that runs. Edit the file in `wef/` without
touching the version and Excel keeps the old manifest forever — the change
looks applied on disk and does nothing, with no error anywhere. This cost a
debugging round on 2026-09-03 when `AutoOpenTaskpane` silently never loaded.
Compare the two files by size when a manifest change appears to have no effect.

**A sideloaded add-in does not put its ribbon tab up on its own, and that is
not fixable from this side.** On a fresh Excel start the **Klikk** tab is
absent until the add-in is activated once from **Add-ins → Developer Add-ins →
Klikk Journals**; after that it stays for the session. Proven 2026-09-03 on
Excel 16.112.3 by a controlled restart: activated at the stable 1.0.2.0
manifest (tab up, pane rendering), saved, quit, relaunched — tab gone. The
mechanism, from `Wef/AppCommands/18.0/` and the diagnostic log:

- Startup ribbon comes only from `Excel.RibbonCache.en-GB`, keyed per identity
  (`<cid>_LiveId`). It listed the four Store add-ins (`wa2000…`) before the
  activation, after it, after quit, and after relaunch. Nothing for
  `5b4e7ec6…` is ever written — there is no `AppCommands/18.0/<hash>/` folder
  for the Registry (developer) catalog at all, only the Omex (Store) one.
- The startup refresh `AppCommands.GetRibbonUpdatesForUserId` reports only
  `OmexIncluded` / `ExCatalogIncluded`; no developer/registry provider takes
  part. `osf.framework` has a `TestGate.RegistryManifestRefresh` — a test gate,
  not something a user can set.
- The manifest bump (1.0.0.0 → 1.0.2.0) is not the cause: the cached manifest
  `Manifests/5b4e7ec6…_1.0.2.0` is re-read at every start (its mtime equals
  the session start), and the test above was at one stable version.
- `Office.Extensibility.UX.UntrustedAddinSkippedFromPersistence` is NOT the
  ribbon signal. Its parent activity is `Office.Excel.Command.FileOpen →
  FileLoad.OpenLoadFile → Coauth.OpenFile`: it fires when a workbook that
  carries an embedded `xl/webextensions/webextension1.xml` reference is
  opened, and it fires identically for the Store Claude add-in
  (`93533c7f…`, StoreType 10). It is about document taskpane persistence.
- Microsoft's own Mac sideload page (learn.microsoft.com, 2026-05) ends with
  "Select Home > Add-ins, and then select your add-in" — it never claims the
  ribbon is restored at start.

The only supported way to have the ribbon tab present at startup is admin
deployment (Integrated Apps / Centralized Deployment in the Microsoft 365
admin center, custom-manifest upload, Mac supported, "automatically appears
in the ribbon"). That needs a work/school tenant with Exchange Online
(Business Basic/Standard/Premium, E1/E3/E5, …) and an Exchange admin. This
Mac's Excel is licensed to a **consumer** account (`IdentityProvider: LiveId`,
`FederationTenantId` all zeros, licence id `CWW_…`, `IsConsumerCopilotUser`),
so that route is closed unless MC signs Excel in to a qualifying tenant.

## Auto-open does not work for a sideloaded add-in — do not re-add it

There is no `AutoOpenTaskpane` extension point in the manifest, and no
`Office.AutoShowTaskpaneWithDocument` checkbox in the pane, on purpose. Both
existed for a few hours on 2026-09-03 and produced a **permanently blank
"Klikk Journals" pane** — Office's pane chrome with nothing inside, not even
the "Loading…" line — stacked next to the working pane the ribbon opens.

What Excel for Mac 16.112 actually does, from
`~/Library/Containers/com.microsoft.Excel/Data/Library/Logs/Diagnostics/EXCEL/Primary*.log`:
on opening a workbook whose webextension part carries the flag it logs
`Office.Extensibility.UX.UntrustedAddinSkippedFromPersistence` (`StoreType: 5`
= developer/Registry, i.e. sideloaded), then `PrepareShowTaskpaneV2` with
`AutoOpenCommandPaneTag: true`, then `Opened Task Pane`, then
`Sandbox.Activation` goes straight to `DetachedActivity_Leaked`. It never logs
`URL Navigation` or `Sandbox.PageLoad`, and the server never receives a request
for `taskpane.html` from that pane. Every auto-open instance that day behaved
this way (07:53, 08:13, 08:26, 08:51, 08:53 UTC); every ribbon-opened pane
navigated and worked. The sideloaded add-in is simply not trusted for
document persistence on this platform, and the "pane" it opens is an empty
webview. That is not fixable from our side short of publishing the add-in
through a catalog or centralized deployment.

`scripts/make_excel_autoopen_template.py` (a default `Book.xltx` carrying the
flag) is gone for the same reason — it put a dead pane on every new workbook.
If a `Book.xltx` is still in
`~/Library/Group Containers/UBF8T346G9.Office/User Content.localized/Startup.localized/Excel/`,
delete it.

**Removing the extension point is NOT enough for a workbook that was saved
while the flag was on.** Verified 2026-09-03 09:08 UTC with the 1.0.2.0
manifest (no `AutoOpenTaskpane`) as the only cached manifest: opening
`Audit FY 2026 View.xlsx` still logged `PrepareShowTaskpaneV2 … AppVersion
1.0.1.0, Visibility true, AutoOpenCommandPaneTag true` and opened a dead pane.
The workbook's own `xl/webextensions/webextension1.xml` (`<we:reference
version="1.0.1.0">` + `Office.AutoShowTaskpaneWithDocument = true`) and
`taskpanes.xml` (`visibility="1"`) drive it. Clear both in the file — set the
property to `false` and `visibility="0"` inside the zip, on a closed copy —
and the same workbook opens with `Visibility false … ShowTaskpane false` and
no pane. That was done to `Audit FY 2026 View.xlsx`; the pre-edit copy is at
`~/Library/Application Support/klikk-backups/`.

If the blank-pane symptom ever comes back: the diagnostic is the Excel log
above, not the served bundle. A pane that rendered `taskpane.html` at all
shows `URL Navigation` in that log and a `GET /excel-addin/taskpane.html`
in the backend container log; a blank one shows neither.

## Right-click a figure → "Klikk: Show transactions"

The manifest adds a `ContextMenu` extension point on `ContextMenuCell`, the
menu Excel opens on a cell right-click. Two entries, both `ShowTaskpane` with
the ribbon's `TaskpaneId` so they reuse the open pane instead of opening a
second one:

- **Klikk: Show transactions** → `taskpane.html#drill`. The pane treats
  `#drill` as "land on Comments, then drill the cell under the cursor": it
  re-reads the sheet binding, resolves the selected cell against the cube /
  PivotTable grid, and runs the same drill the **Show transactions** button
  does (`GET /xero/data/journals/pivot/drill/` → new detail sheet, receipt
  links included). On a sheet that is not one of ours, or on a heading cell,
  it says so in the error line instead of drilling. After the drill the pane
  rewrites its URL to `#comments` so the next right-click is a URL change
  again — a same-URL `ShowTaskpane` may not re-navigate the webview, and
  without a navigation there is no `hashchange` to react to.
- **Klikk: Open Journals pane** → `#query`, the plain pane.

Why `ShowTaskpane` and not `ExecuteFunction`: a function command runs in its
own runtime, separate from the pane, and can neither reach the pane's cube
cache nor its drill/render code without moving the whole add-in onto a shared
runtime. The pane already does everything the drill needs; the menu item only
has to get it there.

Not verified live at the time of writing (2026-09-03): Excel was open on
unsaved workbooks, and a manifest change needs a restart. The manifest
validates (`npx office-addin-manifest validate`) — which the previous one did
not: `<Label>` inside `<CustomTab>` must follow the `<Group>`s per the schema,
and Excel had been tolerating it first. The two things to check on first use:
that the entries appear at all on the cell menu (if not, the cached manifest is
still 1.0.2.0 — see the version rule above), and that a second right-click
drills again (if not, the `#comments` rewrite is not enough and the entry needs
a cache-busting variant).

## Build rewrites the cube sheet in front; New sheet is the other button

A cube sheet carries its own spec inside the workbook
(`Office.context.document.settings`, key `klikkJournalQuery::<worksheet id>`).
The id is the sheet's `xr:uid`, which is written into `xl/worksheets/sheetN.xml`
and survives save / close / reopen — verified 2026-09-03 by unzipping
`Audit FY 2026 View.xlsx`: the three live sheets' `xr:uid`s matched their
binding keys exactly. So on reopen, and on every sheet switch
(`worksheets.onActivated`), the pane reads the binding and points the wells at
that sheet's rows / columns / filters / measure (`syncCubeToSheet`).

Until 2026-09-03 `buildCube` always rendered to a **new** sheet, so a layout
could never be edited: every Build — and every drag with "Rebuild on every
change" ticked — produced Cube 2, Cube 3, Cube 4 … (one workbook had three
bindings written 08:03:33, :42 and :44). Now:

- **Rebuild &lt;sheet&gt;** (the primary button, relabelled whenever the active
  sheet is one of our cubes) rewrites that sheet in place from the wells —
  the PivotTable model. Groups and frozen panes are peeled off before the
  clear, because `Range.clear` leaves both behind.
- **New sheet** appears next to it only when a cube is in front, and always
  creates a fresh sheet.
- With no cube sheet in front the primary button reads **Build cube view**
  and writes a new sheet, as before.
- **Saved views → Rebuild** follows the same rule and says in its message
  whether it went onto the sheet in front or a new one.

Boot order matters for this: `inspectActiveSheet()` must finish before
`connect()` reaches `populateCube()`, or the wells come up as defaults on top
of a cube the pane should have recognised. `Office.onReady` sequences them.

Bindings for deleted sheets are never pruned — the settings API has no key
enumeration — so a much-edited workbook carries a few dead entries. Harmless.

## An empty subset means ALL members, never none

`dimfParam` omits a dimension from `dimf` when its subset is empty, so a field
dragged into rows, columns or filters starts unrestricted and its chip reads
plainly (`Year`) rather than `Year · 0 selected`. Clearing every member in the
subset editor therefore widens back to all — a cube filtered to nothing is not
reachable, deliberately. Keep that: it is the difference between a new field
showing the whole ledger and showing an empty sheet.

## Checks

There was no automated check on this add-in until 2026-09-03, and everything
that broke that day was found by clicking in Excel. These are the cheapest
checks that would have caught those four failures, and nothing more.

```bash
make check      # lint + tests. Run before every ship.
make prove      # reconstruct the four 2026-09-03 bugs; show each check catch one
npm ci          # once, to get eslint + jsdom. No bundler, no build step.
```

`make check` is three gates:

| Gate | What it catches | The failure it is for |
|---|---|---|
| `npm run lint` — ESLint 9 `no-undef`, browser + Office globals | a handler calling a function that no longer exists | `reloadThisSheet` was deleted on 2026-08-20 (`d0efa8f`) with its click handler left calling it: a live `ReferenceError` that shipped for two weeks |
| `test/boot.test.js` — jsdom loads `taskpane.html` with an `Office`/`Excel` stub | the pane not booting; a missing control killing the listeners *after* it; a dead handler, by clicking every button | `wireEvents()` registered 40+ listeners with bare `addEventListener`; one missing id threw and every later listener never attached, while the pane still rendered |
| `test/cube.test.js` — clicks Build five times against a fake workbook | a Build that adds a sheet instead of rewriting the bound one | `Cube, Cube 2 … Cube 5` in eleven seconds of dragging the wells |
| `test/lib.test.js` — pure functions, no DOM | run merging, `dimf`, `rtotals`/`ctotals`, date serials, the Build-target rule | rows keyed on `Math.min(depth, 2)` merged a depth-3 row into a depth-2 run, so the first child of every parent lost its indent |

**`make prove` is the point.** A test that has never failed has proved
nothing, so `test/prove-regressions.js` copies the add-in to a temp directory,
edits the historic bug back in (each mutation names the commit that removed
it), and requires the check that claims to catch it to FAIL — then to pass on
the tree as it stands. It exits non-zero if any check is blind. Run it after
changing a test, and after changing any code a test pins.

The Excel stub in `test/fake-host.js` is deliberately shallow: worksheets,
tables and document settings are modelled because the tests assert on them;
ranges and formats are a Proxy that swallows every call. "Did `.format.font.bold`
get set" is not a bug class this harness is for. If a test needs a range
property, model that property — do not deepen the Proxy.

### Known gap, found by this harness and not yet fixed

The `on()` helper hardened the *listeners* against a missing control, and
`setButtons()` goes through the null-safe `setDisabled()`. `paintRefreshPanel()`
does not: it sets `.disabled` directly on six controls (`app.js:919-924` and
`938-943`) with no null check. Delete
`btnPivot` from the page and `inspectActiveSheet()` rejects on the boot path —
the pane comes up, the Refresh panel is never painted, and nothing says why.
That is the 2026-09-03 failure one function along. It is a `todo` test in
`test/boot.test.js`; deleting `todo: true` is the check that the fix worked.
