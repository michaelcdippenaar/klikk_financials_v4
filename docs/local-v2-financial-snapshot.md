# Local V2 entity-scoped financial snapshot inventory

Status: **blocked pending explicit entity scope and reviewed export plan**.

This is an allowlist design, not an export script. A full database dump or
restore is forbidden. Snapshot artifacts must be encrypted, access-restricted,
stored outside Git/worktrees, have an expiry, and be purged independently from
the synthetic local database.

## Current V2 dependency truth

The released V2 fields `viewerContext`, `overviewIngestSources`,
`ingestOverview`, `xeroPipelineSummary`, history and run detail do **not** query
ledger transactions, journals, invoices, aged reports or cube balances. Their
current read dependency closure is:

| Table | Columns read | Local source |
|---|---|---|
| `users` | `id`, username/name/email, active/password for login | synthetic only |
| `xero_core_xerotenant` | `tenant_id`, `tenant_name`, `fiscal_year_start_month`, `reauth_required` | synthetic scope anchor |
| `web_api_v2_userentitymembership` | user/entity/role/active | synthetic only |
| `web_api_v2_viewerpreference` | default entity/FY | synthetic only |
| `web_api_v2_userentitycapability` | capability/active | deliberately empty |
| `web_api_v2_ingestsourcejobdefinition` | entity/key/family/label/required/configuration/operations/read capabilities/active | synthetic catalogue |
| `web_api_v2_ingestprocessrun` and `...runperiod` | stage status/evidence/history | deliberately empty |
| `xero_auth_xeroclientcredentials` and `...xerotenanttoken` | connection-exists check | deliberately empty |
| `xero_sync_xerolastupdate` | prerequisite success timestamps | deliberately empty |

Consequently, the approved v0.2 snapshot has **no production financial fact
tables**. Copying financial rows today would add risk without changing the V2
UI. A new V2 financial resolver must first identify its query/column closure and
receive a policy-version review.

## Candidate financial dependency groups for a future policy version

The following are inventoried but **not export-authorized**.

### Derived Trial Balance read model (smallest useful future group)

1. `xero_core_xerotenant`
   - preserve only `fiscal_year_start_month`;
   - remap `tenant_id` to `local-v2-entity-0001` and `tenant_name` to the
     invented local label;
   - omit tracking-category source IDs and all reauthorization detail.
2. `xero_metadata_xerobusinessunits`
   - `id`, remapped `organisation_id`, division/business-unit codes;
   - mask descriptions when not required by the resolver.
3. `xero_metadata_xeroaccount`
   - `account_id`, remapped `organisation_id`, `business_unit_id`, reporting
     code/name, grouping, code, name, type, entry/occurrence attributes;
   - exclude `bank_account_number` and raw `collection`.
4. `xero_metadata_xerotracking`
   - `id`, remapped `organisation_id`, locally remapped `option_id` and
     `tracking_category_id`, category slot;
   - mask category/option names unless UI labels require them; exclude
     `collection`.
5. `xero_metadata_xerocontacts` only if the approved resolver groups by contact
   - locally remapped `contacts_id`, remapped `organisation_id`, deterministic
     label such as `Contact 000001`; exclude `collection`.
6. `xero_cube_xerotrailbalance`
   - `id`, remapped organisation/account/contact/tracking FKs, date/year/month,
     `fin_year`, `fin_period`, amount/debit/credit/tax/balance-to-date;
   - select only rows inside the approved resolved FY/month bounds.

FK import order is tenant, business units, accounts/tracking/contacts, then Trial
Balance. Every FK must resolve before commit. Counts and sums must be checked
only inside the approved entity/period scope.

### Journal drill dependency group

Only if a reviewed UI resolver needs journal detail:

- `xero_data_xerotransactionsource`: local ID, remapped organisation/contact,
  date/type/attachment flags; remap source transaction ID and exclude
  `collection`.
- `xero_data_xerojournalssource`: local ID, remapped organisation and journal
  ID, number/type/processed; exclude `collection`.
- `xero_data_xerojournals`: remapped organisation, journal/account/contact,
  tracking and source FKs; date, amount/debit/credit/tax and approved analytical
  fields. Free-text description/reference must be masked or omitted unless the
  specific UI requirement is approved.
- `xero_data_xerojournalexclusion`: remapped entity and journal identifiers,
  date/type/number/active. Description/reference/reason are excluded by default
  because they may contain personal or operational narrative.

This group depends on the metadata group and is not needed by current V2.

### Invoice/AP/AR dependency group

Only if a reviewed workbench resolver exists:

- `xero_data_xeroinvoice`: remapped entity/invoice/contact IDs; type/status and
  date/due/payment dates; currency/rate and numeric totals; boolean state;
  exclude reference, URL, branding ID, contact name and raw `collection` unless
  explicitly justified.
- `xero_data_xeroinvoicelineitem`: remapped invoice/account/tracking FKs;
  quantity/unit/tax/line/discount amounts and position; mask description and
  item code unless required.
- `xero_data_agedpayable` and `xero_data_agedreceivable`: remapped tenant/contact
  IDs, report date, ageing buckets and total; replace contact name with a
  deterministic pseudonym.

These tables are not current V2 dependencies and remain denied in v0.2.

## Absolute exclusions

The machine-readable denylist is
`config/local-v2-financial-snapshot-policy.json`. It excludes production users,
password hashes, permissions, sessions, JWT outstanding/blacklist rows, DRF
tokens, OAuth client/tenant credentials, source tokens, API/service secrets,
memberships, grants/capabilities, preferences, process runs/periods/audits,
scheduler/task/API-call logs, deployment/webhook state, raw source JSON fields,
documents/files and environment files.

## Reviewed export/import protocol (future only)

1. User confirms exactly one entity or all three and an explicit FY/month range.
2. Backend updates the policy version with the exact resolver-backed table and
   column allowlist; QA reviews masking and FK closure.
3. DevOps performs a read-only production export using entity predicates on
   every table. No unrestricted `pg_dump` is used.
4. IDs are deterministically remapped before the artifact leaves the restricted
   export environment. Personal names/free text are omitted or pseudonymized.
5. Rows are serialized in stable PK order. A manifest records policy version,
   scope, row counts, SHA-256 per file, FK checks, creation/expiry and tool
   version. Numeric control totals may be recorded separately without row data.
6. The artifact is encrypted, mode 0600, outside all repositories/worktrees.
7. Import refuses an unknown policy version, unexpected table/column, checksum
   mismatch, nonlocal DB name, nonempty target fact tables or unresolved FK.
8. Post-import counts/checksums/FKs and resolver results are compared with the
   manifest. Synthetic authentication and VIEW-only membership remain.
9. Purge removes the exact imported rows by snapshot ID, the exact encrypted
   artifact and derived caches; teardown removes the named local volume. Counts
   must return to the deterministic synthetic baseline.

No step in this protocol is authorized until entity scope and the export plan
are approved.

