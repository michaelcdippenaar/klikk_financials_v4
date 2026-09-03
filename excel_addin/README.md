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
