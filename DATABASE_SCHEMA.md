# Klikk Business Intelligence — Database Schema

**Database:** `klikk_financials_v4` (PostgreSQL 14)
**Engine:** Django 5.x ORM
**Tables:** 76
**Extensions:** pgvector

---

## Table of Contents

1. [User & Auth](#1-user--auth)
2. [Xero Core](#2-xero-core)
3. [Xero Auth](#3-xero-auth)
4. [Xero Metadata](#4-xero-metadata)
5. [Xero Data](#5-xero-data)
6. [Xero Sync](#6-xero-sync)
7. [Xero Cube](#7-xero-cube)
8. [Xero Validation](#8-xero-validation)
9. [Investec](#9-investec)
10. [Financial Investments](#10-financial-investments)
11. [Planning Analytics (TM1)](#11-planning-analytics-tm1)
12. [AI Agent](#12-ai-agent)
13. [Journal Push](#13-journal-push)
14. [Entity Relationship Diagram](#14-entity-relationship-diagram)
15. [Foreign Key Reference](#15-foreign-key-reference)

---

## 1. User & Auth

### `users`
Custom user model extending Django AbstractUser.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| password | varchar(128) | NO | |
| last_login | timestamptz | YES | |
| is_superuser | boolean | NO | |
| username | varchar(150) | NO | UNIQUE |
| first_name | varchar(150) | NO | |
| last_name | varchar(150) | NO | |
| email | varchar(254) | NO | |
| is_staff | boolean | NO | |
| is_active | boolean | NO | |
| date_joined | timestamptz | NO | |
| created_at | timestamptz | NO | auto |
| updated_at | timestamptz | NO | auto |

### `users_groups`
M2M: User ↔ auth_group

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| user_id | bigint | NO | FK → users(id) |
| group_id | integer | NO | FK → auth_group(id) |

### `users_user_permissions`
M2M: User ↔ auth_permission

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| user_id | bigint | NO | FK → users(id) |
| permission_id | integer | NO | FK → auth_permission(id) |

### `authtoken_token`
DRF token authentication.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **key** | varchar(40) | NO | PK |
| created | timestamptz | NO | |
| user_id | bigint | NO | FK → users(id), UNIQUE |

### `auth_group`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | integer | NO | PK |
| name | varchar(150) | NO | UNIQUE |

### `auth_group_permissions`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| group_id | integer | NO | FK → auth_group(id) |
| permission_id | integer | NO | FK → auth_permission(id) |

### `auth_permission`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | integer | NO | PK |
| name | varchar(255) | NO | |
| content_type_id | integer | NO | FK → django_content_type(id) |
| codename | varchar(100) | NO | |

---

## 2. Xero Core

### `xero_core_xerotenant`
Central tenant/organisation table. Most Xero tables reference this.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **tenant_id** | varchar(100) | NO | PK (Xero org ID) |
| tenant_name | varchar(100) | NO | |
| tracking_category_1_id | varchar(64) | YES | Xero TrackingCategoryID slot 1 |
| tracking_category_2_id | varchar(64) | YES | Xero TrackingCategoryID slot 2 |
| fiscal_year_start_month | integer | YES | 1-12, default 7 (July) |

---

## 3. Xero Auth

### `xero_auth_xeroclientcredentials`
OAuth2 credentials per user.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| user_id | bigint | NO | FK → users(id) |
| client_id | varchar(100) | NO | |
| client_secret | varchar(100) | NO | |
| scope | jsonb | NO | |
| token | jsonb | YES | Legacy |
| refresh_token | varchar(1000) | YES | Legacy |
| expires_at | timestamptz | YES | Legacy |
| tenant_tokens | jsonb | NO | {tenant_id: {token, refresh_token, ...}} |
| active | boolean | NO | |

### `xero_auth_xerotenanttoken`
Per-tenant OAuth tokens.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| tenant_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| credentials_id | bigint | NO | FK → xero_auth_xeroclientcredentials(id) |
| token | jsonb | NO | |
| refresh_token | varchar(1000) | NO | |
| expires_at | timestamptz | NO | |
| connected_at | timestamptz | NO | auto |

UNIQUE: (tenant_id, credentials_id)

### `xero_auth_xeroauthsettings`
OAuth URL configuration.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| access_token_url | varchar(255) | NO | |
| refresh_url | varchar(255) | NO | |
| auth_url | varchar(255) | NO | |

---

## 4. Xero Metadata

### `xero_metadata_xerobusinessunits`
Business unit / division mapping per organisation.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| division_code | varchar(1) | YES | |
| business_unit_code | varchar(1) | NO | |
| division_description | varchar(100) | YES | |
| business_unit_description | varchar(100) | NO | |

UNIQUE: (organisation_id, business_unit_code, division_code)

### `xero_metadata_xeroaccount`
Chart of accounts synced from Xero.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **account_id** | varchar(40) | NO | PK (Xero AccountID) |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| business_unit_id | bigint | YES | FK → xero_metadata_xerobusinessunits(id) |
| reporting_code | text | NO | |
| reporting_code_name | text | NO | |
| bank_account_number | varchar(40) | YES | |
| grouping | varchar(30) | NO | |
| code | varchar(10) | NO | |
| name | varchar(150) | NO | |
| type | varchar(30) | NO | |
| collection | jsonb | YES | Raw Xero JSON |
| attr_entry_type | varchar(30) | YES | |
| attr_occurrence | varchar(30) | YES | |

### `xero_metadata_xerocontacts`
Contacts synced from Xero.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **contacts_id** | varchar(55) | NO | PK (Xero ContactID) |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| name | text | NO | |
| collection | jsonb | YES | Raw Xero JSON |

### `xero_metadata_xerotracking`
Tracking categories and options synced from Xero.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| option_id | text | NO | |
| name | text | YES | Category name |
| option | text | YES | Option name |
| collection | jsonb | YES | Raw Xero JSON |
| tracking_category_id | varchar(64) | YES | |
| category_slot | smallint | YES | 1 or 2 |

---

## 5. Xero Data

### `xero_data_xerotransactionsource`
Raw transaction data from Xero (invoices, payments, bank transactions, etc.).

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| transactions_id | varchar(55) | NO | Xero transaction ID |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| contact_id | varchar(55) | YES | FK → xero_metadata_xerocontacts(contacts_id) |
| date | date | YES | |
| type | varchar(30) | NO | |
| collection | jsonb | YES | Raw Xero JSON |

### `xero_data_xerodocument`
Attachments/documents linked to transactions.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| transaction_source_id | bigint | YES | FK → xero_data_xerotransactionsource(id) |
| document_id | varchar(55) | NO | |
| file_name | varchar(255) | NO | |
| mime_type | varchar(100) | NO | |
| url | text | NO | |
| file_size | integer | YES | |
| file_path | varchar(500) | YES | |
| collection | jsonb | YES | |

### `xero_data_xerojournalssource`
Raw journal data from Xero.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| journal_id | varchar(55) | NO | |
| journal_number | integer | YES | |
| journal_date | date | YES | |
| source_type | varchar(50) | NO | |
| reference | text | YES | |
| collection | jsonb | YES | |

### `xero_data_xerojournals`
Processed journal line entries with account, contact, and tracking dimensions.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| journal_source_id | bigint | YES | FK → xero_data_xerojournalssource(id) |
| transaction_source_id | varchar(55) | YES | FK → xero_data_xerotransactionsource(transactions_id) |
| account_id | varchar(40) | YES | FK → xero_metadata_xeroaccount(account_id) |
| contact_id | varchar(55) | YES | FK → xero_metadata_xerocontacts(contacts_id) |
| tracking1_id | bigint | YES | FK → xero_metadata_xerotracking(id) |
| tracking2_id | bigint | YES | FK → xero_metadata_xerotracking(id) |
| journal_date | date | YES | |
| debit | numeric(14,2) | NO | |
| credit | numeric(14,2) | NO | |
| source_type | varchar(50) | YES | |
| description | text | YES | |

---

## 6. Xero Sync

### `xero_sync_xerolastupdate`
Tracks the last sync timestamp per Xero endpoint per organisation.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| end_point | varchar(100) | NO | |
| name | varchar(200) | YES | |
| date | timestamptz | YES | Last update timestamp |

### `xero_sync_xerotenantschedule`
Per-tenant sync scheduling configuration.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| tenant_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| enabled | boolean | NO | |
| update_interval_minutes | integer | NO | |
| update_start_time | time | NO | |
| last_update_run | timestamptz | YES | |
| last_process_run | timestamptz | YES | |
| next_update_run | timestamptz | YES | |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `xero_sync_xeroapicalllog`
API call tracking per process.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| tenant_id | varchar(100) | YES | FK → xero_core_xerotenant(tenant_id) |
| process | varchar(50) | NO | |
| api_calls | integer | NO | |
| created_at | timestamptz | NO | |

### `xero_sync_xerotaskexecutionlog`
Sync task execution history.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| tenant_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| task_type | varchar(20) | NO | |
| status | varchar(20) | NO | |
| started_at | timestamptz | NO | |
| completed_at | timestamptz | YES | |
| duration_seconds | double precision | YES | |
| records_processed | integer | YES | |
| error_message | text | YES | |
| stats | jsonb | NO | |
| created_at | timestamptz | NO | |

### `xero_sync_trigger`
Triggers (condition, schedule, event, custom) for process trees.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| name | varchar(200) | NO | |
| trigger_type | varchar(50) | NO | |
| enabled | boolean | NO | |
| state | varchar(20) | NO | |
| description | text | NO | |
| configuration | jsonb | NO | |
| process_tree_id | bigint | YES | FK → xero_sync_processtree(id) |
| xero_last_update_id | bigint | YES | FK → xero_sync_xerolastupdate(id) |
| last_checked | timestamptz | YES | |
| last_triggered | timestamptz | YES | |
| last_fired_manually | timestamptz | YES | |
| trigger_count | integer | NO | |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `xero_sync_processtree`
Process tree definitions (DAG of sync operations).

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| name | varchar(100) | NO | |
| description | text | NO | |
| process_tree_data | jsonb | NO | Tree structure |
| response_variables | jsonb | NO | |
| cache_enabled | boolean | NO | |
| enabled | boolean | NO | |
| trigger_id | bigint | YES | FK → xero_sync_trigger(id) |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `xero_sync_processtree_dependent_trees`
M2M: ProcessTree dependencies.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| from_processtree_id | bigint | NO | FK → xero_sync_processtree(id) |
| to_processtree_id | bigint | NO | FK → xero_sync_processtree(id) |

### `xero_sync_processtree_sibling_trees`
M2M: ProcessTree siblings (parallel execution).

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| from_processtree_id | bigint | NO | FK → xero_sync_processtree(id) |
| to_processtree_id | bigint | NO | FK → xero_sync_processtree(id) |

### `xero_sync_processtreeschedule`
Scheduling config for process trees.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| process_tree_id | bigint | NO | FK → xero_sync_processtree(id) |
| enabled | boolean | NO | |
| interval_minutes | integer | NO | |
| start_time | time | NO | |
| last_run | timestamptz | YES | |
| next_run | timestamptz | YES | |
| context | jsonb | NO | |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

---

## 7. Xero Cube

Pre-aggregated financial data for reporting.

### `xero_cube_xerotrailbalance`
Trail balance by account, contact, and tracking dimensions.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| account_id | varchar(40) | NO | FK → xero_metadata_xeroaccount(account_id) |
| contact_id | varchar(55) | YES | FK → xero_metadata_xerocontacts(contacts_id) |
| tracking1_id | bigint | YES | FK → xero_metadata_xerotracking(id) |
| tracking2_id | bigint | YES | FK → xero_metadata_xerotracking(id) |
| year | integer | NO | |
| month | integer | NO | |
| debit | numeric(14,2) | NO | |
| credit | numeric(14,2) | NO | |

### `xero_cube_xeropnlbytracking`
P&L by tracking option.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| account_id | varchar(40) | NO | FK → xero_metadata_xeroaccount(account_id) |
| tracking_id | bigint | YES | FK → xero_metadata_xerotracking(id) |
| year | integer | NO | |
| month | integer | NO | |
| debit | numeric(14,2) | NO | |
| credit | numeric(14,2) | NO | |

### `xero_cube_xerobalancesheet`
Balance sheet aggregation.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| account_id | varchar(40) | NO | FK → xero_metadata_xeroaccount(account_id) |
| contact_id | varchar(55) | YES | FK → xero_metadata_xerocontacts(contacts_id) |
| year | integer | NO | |
| month | integer | NO | |
| debit | numeric(14,2) | NO | |
| credit | numeric(14,2) | NO | |

---

## 8. Xero Validation

Report-vs-database comparison for data integrity.

### `xero_validation_xerotrailbalancereport`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| report_date | date | NO | |
| report_type | varchar(50) | NO | |
| imported_at | timestamptz | NO | |
| raw_data | jsonb | YES | |
| parsed_json | jsonb | YES | |

### `xero_validation_xerotrailbalancereportline`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| report_id | bigint | NO | FK → xero_validation_xerotrailbalancereport(id) |
| account_id | varchar(40) | YES | FK → xero_metadata_xeroaccount(account_id) |
| account_code | varchar(50) | NO | |
| account_name | varchar(255) | NO | |
| account_type | varchar(50) | YES | |
| row_type | varchar(50) | YES | |
| debit | numeric | NO | |
| credit | numeric | NO | |
| value | numeric | NO | |
| period_debit | numeric | NO | |
| period_credit | numeric | NO | |
| ytd_debit | numeric | NO | |
| ytd_credit | numeric | NO | |
| db_value | numeric | YES | |
| raw_cell_data | jsonb | YES | |

### `xero_validation_trailbalancecomparison`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| report_id | bigint | NO | FK → xero_validation_xerotrailbalancereport(id) |
| account_id | varchar(40) | NO | FK → xero_metadata_xeroaccount(account_id) |
| xero_value | numeric | NO | |
| db_value | numeric | NO | |
| difference | numeric | NO | |
| match_status | varchar(20) | NO | |
| notes | text | NO | |
| compared_at | timestamptz | NO | |

### `xero_validation_xeroprofitandlossreport`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| from_date | date | NO | |
| to_date | date | NO | |
| periods | integer | NO | |
| timeframe | varchar(20) | NO | |
| imported_at | timestamptz | NO | |
| raw_data | jsonb | YES | |

### `xero_validation_xeroprofitandlossreportline`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| report_id | bigint | NO | FK → xero_validation_xeroprofitandlossreport(id) |
| account_id | varchar(40) | YES | FK → xero_metadata_xeroaccount(account_id) |
| account_code | varchar(50) | NO | |
| account_name | varchar(255) | NO | |
| account_type | varchar(50) | YES | |
| row_type | varchar(50) | NO | |
| section_title | varchar(255) | NO | |
| period_values | jsonb | NO | |
| raw_cell_data | jsonb | YES | |

### `xero_validation_profitandlosscomparison`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| report_id | bigint | NO | FK → xero_validation_xeroprofitandlossreport(id) |
| account_id | varchar(40) | NO | FK → xero_metadata_xeroaccount(account_id) |
| period_index | integer | NO | |
| period_date | date | NO | |
| xero_value | numeric | NO | |
| db_value | numeric | NO | |
| difference | numeric | NO | |
| match_status | varchar(20) | NO | |
| notes | text | NO | |
| compared_at | timestamptz | NO | |

---

## 9. Investec

### `investec_investecjsesharenamemapping`
Maps share names to company/share codes.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| share_name | varchar(200) | NO | |
| company_name | varchar(200) | NO | |
| share_code | varchar(20) | NO | |

### `investec_investecjsetransaction`
JSE (stock exchange) transactions.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| date | date | NO | |
| share_name | varchar(200) | NO | |
| transaction_type | varchar(50) | NO | |
| quantity | numeric | NO | |
| price | numeric | NO | |
| brokerage | numeric | NO | |
| amount | numeric | NO | |
| collection | jsonb | YES | |

### `investec_investecjseportfolio`
JSE portfolio data.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| share_name | varchar(200) | NO | |
| quantity | numeric | NO | |
| average_cost | numeric | NO | |
| market_value | numeric | NO | |
| collection | jsonb | YES | |

### `investec_investecjsesharemonthlyperformance`
Monthly share performance snapshots.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| share_name | varchar(200) | NO | |
| year | integer | NO | |
| month | integer | NO | |
| open_price | numeric | YES | |
| close_price | numeric | YES | |
| high_price | numeric | YES | |
| low_price | numeric | YES | |
| volume | bigint | YES | |
| collection | jsonb | YES | |

### `investec_investecbankaccount`
Investec bank accounts.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| account_id | varchar(100) | NO | UNIQUE |
| account_number | varchar(50) | NO | |
| account_name | varchar(200) | NO | |
| reference_name | varchar(200) | NO | |
| product_name | varchar(200) | NO | |
| collection | jsonb | YES | |

### `investec_investecbanktransaction`
Bank transactions from Investec API.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| account_id | bigint | NO | FK → investec_investecbankaccount(id) |
| transaction_id | varchar(100) | NO | |
| type | varchar(50) | NO | |
| status | varchar(50) | NO | |
| description | text | NO | |
| amount | numeric | NO | |
| running_balance | numeric | YES | |
| posting_date | date | NO | |
| value_date | date | YES | |
| action_date | date | YES | |
| collection | jsonb | YES | |

### `investec_investecbanktransactioncontext`
Enriched context for bank transactions (beneficiary, Xero contact mapping).

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| transaction_id | bigint | NO | FK → investec_investecbanktransaction(id) |
| beneficiary_id | bigint | YES | FK → investec_investecbeneficiary(id) |
| xero_contact_id | varchar(55) | YES | FK → xero_metadata_xerocontacts(contacts_id) |

### `investec_investecbeneficiary`
Investec beneficiaries.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| account_id | bigint | NO | FK → investec_investecbankaccount(id) |
| beneficiary_id | varchar(100) | NO | |
| name | varchar(200) | NO | |
| bank_name | varchar(200) | YES | |
| account_number | varchar(50) | YES | |
| collection | jsonb | YES | |

### `investec_investecbanksynclog`
Sync history for bank data.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| sync_type | varchar(50) | NO | |
| status | varchar(20) | NO | |
| started_at | timestamptz | NO | |
| completed_at | timestamptz | YES | |
| records_synced | integer | YES | |
| error_message | text | YES | |

---

## 10. Financial Investments

### `financial_investments_symbol`
Ticker symbols tracked in the system.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol | varchar(20) | NO | UNIQUE |
| name | varchar(255) | NO | |
| exchange | varchar(50) | NO | |
| category | varchar(20) | NO | |
| share_name_mapping_id | bigint | YES | FK → investec_investecjsesharenamemapping(id) |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `financial_investments_pricepoint`
Daily OHLCV price data.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| date | date | NO | |
| open | numeric | NO | |
| high | numeric | NO | |
| low | numeric | NO | |
| close | numeric | NO | |
| volume | bigint | YES | |
| adjusted_close | numeric | YES | |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

UNIQUE: (symbol_id, date)

### `financial_investments_dividend`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| date | date | NO | |
| amount | numeric | NO | |
| currency | varchar(10) | NO | |

### `financial_investments_split`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| date | date | NO | |
| ratio | numeric | NO | |

### `financial_investments_symbolinfo`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id), UNIQUE |
| fetched_at | timestamptz | NO | |
| data | jsonb | NO | |

### `financial_investments_financialstatement`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| statement_type | varchar(20) | NO | income/balance/cash_flow |
| period_end | date | YES | |
| freq | varchar(20) | NO | annual/quarterly |
| data | jsonb | NO | |
| fetched_at | timestamptz | NO | |

### `financial_investments_earningsreport`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| period_end | date | YES | |
| freq | varchar(20) | NO | |
| data | jsonb | NO | |
| fetched_at | timestamptz | NO | |

### `financial_investments_earningsestimate`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| fetched_at | timestamptz | NO | |
| data | jsonb | NO | |

### `financial_investments_analystrecommendation`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| fetched_at | timestamptz | NO | |
| data | jsonb | NO | |

### `financial_investments_analystpricetarget`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| fetched_at | timestamptz | NO | |
| data | jsonb | NO | |

### `financial_investments_ownershipsnapshot`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| holder_type | varchar(30) | NO | institutional/major/insider |
| fetched_at | timestamptz | NO | |
| data | jsonb | NO | |

### `financial_investments_newsitem`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| symbol_id | bigint | NO | FK → financial_investments_symbol(id) |
| title | varchar(500) | NO | |
| link | varchar(1000) | NO | |
| published_at | timestamptz | YES | |
| publisher | varchar(200) | NO | |
| summary | text | NO | |
| data | jsonb | NO | |
| created_at | timestamptz | NO | |

### `financial_investments_watchlisttablepreference`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| user_id | bigint | NO | FK → users(id) |
| table_key | varchar(100) | NO | |
| visible_columns | jsonb | NO | |
| column_order | jsonb | NO | |

---

## 11. Planning Analytics (TM1)

### `planning_analytics_tm1serverconfig`
Active TM1 server connection (singleton pattern).

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| base_url | varchar(500) | NO | |
| username | varchar(200) | NO | |
| password | varchar(200) | NO | |
| is_active | boolean | NO | Only one active at a time |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `planning_analytics_tm1processconfig`
TI processes available for pipeline execution.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| process_name | varchar(300) | NO | |
| enabled | boolean | NO | |
| sort_order | smallint | NO | |
| parameters | jsonb | NO | |

---

## 12. AI Agent

### `ai_agent_knowledgecorpus`
Named knowledge bases.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| slug | varchar(120) | NO | UNIQUE |
| name | varchar(255) | NO | |
| description | text | NO | |
| is_active | boolean | NO | |
| created_by_id | bigint | YES | FK → users(id) |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `ai_agent_systemdocument`
Markdown documents in the knowledge base.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| slug | varchar(120) | NO | UNIQUE |
| title | varchar(255) | NO | |
| content_markdown | text | NO | |
| metadata | jsonb | NO | |
| is_active | boolean | NO | |
| pin_to_context | boolean | NO | |
| context_order | integer | NO | |
| corpus_id | bigint | YES | FK → ai_agent_knowledgecorpus(id) |
| project_id | bigint | YES | FK → ai_agent_agentproject(id) |
| created_by_id | bigint | YES | FK → users(id) |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `ai_agent_knowledgechunkembedding`
Vector embeddings for semantic search.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| corpus_id | bigint | NO | FK → ai_agent_knowledgecorpus(id) |
| system_document_id | bigint | NO | FK → ai_agent_systemdocument(id) |
| project_id | bigint | YES | FK → ai_agent_agentproject(id) |
| embedding_model | varchar(120) | NO | |
| source_hash | varchar(64) | NO | |
| chunk_index | integer | NO | |
| chunk_text | text | NO | |
| embedding | jsonb | NO | Vector data |
| embedded_at | timestamptz | NO | |

UNIQUE: (system_document_id, chunk_index, embedding_model)

### `ai_agent_agentproject`
Agent project groupings.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| slug | varchar(120) | NO | UNIQUE |
| name | varchar(255) | NO | |
| description | text | NO | |
| memory | jsonb | NO | |
| is_active | boolean | NO | |
| default_corpus_id | bigint | YES | FK → ai_agent_knowledgecorpus(id) |
| created_by_id | bigint | YES | FK → users(id) |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `ai_agent_agentsession`
Chat sessions.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| title | varchar(255) | NO | |
| status | varchar(20) | NO | |
| memory | jsonb | NO | |
| organisation_id | varchar(100) | YES | FK → xero_core_xerotenant(tenant_id) |
| project_id | bigint | YES | FK → ai_agent_agentproject(id) |
| created_by_id | bigint | YES | FK → users(id) |
| created_at | timestamptz | NO | |
| updated_at | timestamptz | NO | |

### `ai_agent_agentmessage`
Messages within a session.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| session_id | bigint | NO | FK → ai_agent_agentsession(id) |
| role | varchar(20) | NO | user/assistant/system |
| content | text | NO | |
| metadata | jsonb | NO | |
| created_by_id | bigint | YES | FK → users(id) |
| created_at | timestamptz | NO | |

### `ai_agent_agenttoolexecutionlog`
Tool invocations by the agent.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| session_id | bigint | YES | FK → ai_agent_agentsession(id) |
| message_id | bigint | YES | FK → ai_agent_agentmessage(id) |
| tool_name | varchar(120) | NO | |
| status | varchar(20) | NO | |
| input_payload | jsonb | NO | |
| output_payload | jsonb | NO | |
| error_message | text | NO | |
| executed_by_id | bigint | YES | FK → users(id) |
| started_at | timestamptz | NO | |
| finished_at | timestamptz | YES | |

### `ai_agent_agentapprovalrequest`
Human-in-the-loop approval for agent actions.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| session_id | bigint | NO | FK → ai_agent_agentsession(id) |
| tool_execution_id | bigint | YES | FK → ai_agent_agenttoolexecutionlog(id), UNIQUE |
| action_name | varchar(120) | NO | |
| payload | jsonb | NO | |
| status | varchar(20) | NO | pending/approved/rejected |
| review_note | text | NO | |
| requested_by_id | bigint | YES | FK → users(id) |
| reviewed_by_id | bigint | YES | FK → users(id) |
| reviewed_at | timestamptz | YES | |
| created_at | timestamptz | NO | |

### `ai_agent_glossaryrefreshrequest`
Requests to refresh glossary embeddings.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| requested_at | timestamptz | NO | |
| organisation_id | integer | YES | |

---

## 13. Journal Push

### `journal_push_accountmapping`
Mapping between Investec bank accounts and Xero accounts for automated journal creation.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| xero_bank_account_id | varchar(40) | NO | FK → xero_metadata_xeroaccount(account_id) |
| xero_category_account_id | varchar(40) | NO | FK → xero_metadata_xeroaccount(account_id) |

### `journal_push_log`
Log of journals pushed to Xero from Investec transactions.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| **id** | bigint | NO | PK |
| organisation_id | varchar(100) | NO | FK → xero_core_xerotenant(tenant_id) |
| investec_transaction_id | bigint | NO | FK → investec_investecjsetransaction(id) |

---

## 14. Entity Relationship Diagram

```
                                    ┌─────────────────────┐
                                    │       users         │
                                    │  (Custom User)      │
                                    └──────────┬──────────┘
                    ┌──────────────────────────┼──────────────────────────┐
                    │                          │                          │
            ┌───────▼────────┐    ┌────────────▼──────────┐    ┌────────▼──────────┐
            │  authtoken_    │    │  xero_auth_xero       │    │  ai_agent_        │
            │  token         │    │  clientcredentials    │    │  agentproject     │
            └────────────────┘    └────────────┬──────────┘    └────────┬──────────┘
                                               │                        │
                                  ┌────────────▼──────────┐    ┌────────▼──────────┐
                                  │  xero_auth_xero       │    │  ai_agent_        │
                                  │  tenanttoken          │    │  agentsession     │
                                  └────────────┬──────────┘    └────────┬──────────┘
                                               │                        │
                              ┌────────────────▼────────────┐  ┌────────▼──────────┐
                              │   xero_core_xerotenant     │  │  ai_agent_        │
                              │   (Central Org Hub)        │  │  agentmessage     │
                              └──────┬──────┬──────┬───────┘  └───────────────────┘
                    ┌────────────────┼──────┼──────┼────────────────┐
                    │                │      │      │                │
           ┌────────▼──────┐  ┌─────▼──┐ ┌▼────┐ ┌▼──────┐  ┌─────▼──────┐
           │  xero_        │  │ xero_  │ │xero_│ │xero_  │  │  xero_     │
           │  metadata_    │  │ data_  │ │sync_│ │cube_  │  │  validation│
           │  xeroaccount  │  │ xero   │ │     │ │       │  │            │
           │  xerocontacts │  │ trans  │ │     │ │       │  │            │
           │  xerotracking │  │ source │ │     │ │       │  │            │
           │  xerobusiness │  │        │ │     │ │       │  │            │
           └───────────────┘  └────────┘ └─────┘ └───────┘  └────────────┘

                              ┌────────────────────────────┐
                              │   investec_investec        │
                              │   bankaccount              │
                              └──────────┬─────────────────┘
                    ┌────────────────────┼──────────────────┐
           ┌────────▼────────┐  ┌────────▼────────┐  ┌─────▼──────────┐
           │  investec_      │  │  investec_      │  │  investec_     │
           │  banktransaction│  │  beneficiary    │  │  banksynclog   │
           └────────┬────────┘  └─────────────────┘  └────────────────┘
                    │
           ┌────────▼────────────┐
           │  investec_          │
           │  banktransaction    │
           │  context            │
           └─────────────────────┘

     ┌──────────────────────┐
     │  financial_          │
     │  investments_symbol  │───┬──► pricepoint, dividend, split, symbolinfo
     └──────────────────────┘   ├──► financialstatement, earningsreport
                                ├──► earningsestimate, analystrecommendation
                                ├──► analystpricetarget, ownershipsnapshot
                                └──► newsitem
```

---

## 15. Foreign Key Reference

| From Table | Column | → To Table | Column |
|------------|--------|------------|--------|
| ai_agent_agentapprovalrequest | requested_by_id | users | id |
| ai_agent_agentapprovalrequest | reviewed_by_id | users | id |
| ai_agent_agentapprovalrequest | session_id | ai_agent_agentsession | id |
| ai_agent_agentapprovalrequest | tool_execution_id | ai_agent_agenttoolexecutionlog | id |
| ai_agent_agentmessage | created_by_id | users | id |
| ai_agent_agentmessage | session_id | ai_agent_agentsession | id |
| ai_agent_agentproject | created_by_id | users | id |
| ai_agent_agentproject | default_corpus_id | ai_agent_knowledgecorpus | id |
| ai_agent_agentsession | created_by_id | users | id |
| ai_agent_agentsession | organisation_id | xero_core_xerotenant | tenant_id |
| ai_agent_agentsession | project_id | ai_agent_agentproject | id |
| ai_agent_agenttoolexecutionlog | executed_by_id | users | id |
| ai_agent_agenttoolexecutionlog | message_id | ai_agent_agentmessage | id |
| ai_agent_agenttoolexecutionlog | session_id | ai_agent_agentsession | id |
| ai_agent_knowledgechunkembedding | corpus_id | ai_agent_knowledgecorpus | id |
| ai_agent_knowledgechunkembedding | project_id | ai_agent_agentproject | id |
| ai_agent_knowledgechunkembedding | system_document_id | ai_agent_systemdocument | id |
| ai_agent_knowledgecorpus | created_by_id | users | id |
| ai_agent_systemdocument | corpus_id | ai_agent_knowledgecorpus | id |
| ai_agent_systemdocument | created_by_id | users | id |
| ai_agent_systemdocument | project_id | ai_agent_agentproject | id |
| financial_investments_* (all) | symbol_id | financial_investments_symbol | id |
| financial_investments_symbol | share_name_mapping_id | investec_investecjsesharenamemapping | id |
| investec_investecbanktransaction | account_id | investec_investecbankaccount | id |
| investec_investecbanktransactioncontext | transaction_id | investec_investecbanktransaction | id |
| investec_investecbanktransactioncontext | beneficiary_id | investec_investecbeneficiary | id |
| investec_investecbanktransactioncontext | xero_contact_id | xero_metadata_xerocontacts | contacts_id |
| investec_investecbeneficiary | account_id | investec_investecbankaccount | id |
| journal_push_accountmapping | organisation_id | xero_core_xerotenant | tenant_id |
| journal_push_accountmapping | xero_bank_account_id | xero_metadata_xeroaccount | account_id |
| journal_push_accountmapping | xero_category_account_id | xero_metadata_xeroaccount | account_id |
| journal_push_log | organisation_id | xero_core_xerotenant | tenant_id |
| journal_push_log | investec_transaction_id | investec_investecjsetransaction | id |
| xero_auth_xeroclientcredentials | user_id | users | id |
| xero_auth_xerotenanttoken | tenant_id | xero_core_xerotenant | tenant_id |
| xero_auth_xerotenanttoken | credentials_id | xero_auth_xeroclientcredentials | id |
| xero_cube_xerotrailbalance | organisation_id | xero_core_xerotenant | tenant_id |
| xero_cube_xerotrailbalance | account_id | xero_metadata_xeroaccount | account_id |
| xero_cube_xerotrailbalance | contact_id | xero_metadata_xerocontacts | contacts_id |
| xero_cube_xerotrailbalance | tracking1_id | xero_metadata_xerotracking | id |
| xero_cube_xerotrailbalance | tracking2_id | xero_metadata_xerotracking | id |
| xero_cube_xeropnlbytracking | organisation_id | xero_core_xerotenant | tenant_id |
| xero_cube_xeropnlbytracking | account_id | xero_metadata_xeroaccount | account_id |
| xero_cube_xeropnlbytracking | tracking_id | xero_metadata_xerotracking | id |
| xero_cube_xerobalancesheet | organisation_id | xero_core_xerotenant | tenant_id |
| xero_cube_xerobalancesheet | account_id | xero_metadata_xeroaccount | account_id |
| xero_cube_xerobalancesheet | contact_id | xero_metadata_xerocontacts | contacts_id |
| xero_data_xerotransactionsource | organisation_id | xero_core_xerotenant | tenant_id |
| xero_data_xerotransactionsource | contact_id | xero_metadata_xerocontacts | contacts_id |
| xero_data_xerodocument | organisation_id | xero_core_xerotenant | tenant_id |
| xero_data_xerodocument | transaction_source_id | xero_data_xerotransactionsource | id |
| xero_data_xerojournalssource | organisation_id | xero_core_xerotenant | tenant_id |
| xero_data_xerojournals | organisation_id | xero_core_xerotenant | tenant_id |
| xero_data_xerojournals | journal_source_id | xero_data_xerojournalssource | id |
| xero_data_xerojournals | transaction_source_id | xero_data_xerotransactionsource | transactions_id |
| xero_data_xerojournals | account_id | xero_metadata_xeroaccount | account_id |
| xero_data_xerojournals | contact_id | xero_metadata_xerocontacts | contacts_id |
| xero_data_xerojournals | tracking1_id | xero_metadata_xerotracking | id |
| xero_data_xerojournals | tracking2_id | xero_metadata_xerotracking | id |
| xero_metadata_xeroaccount | organisation_id | xero_core_xerotenant | tenant_id |
| xero_metadata_xeroaccount | business_unit_id | xero_metadata_xerobusinessunits | id |
| xero_metadata_xerobusinessunits | organisation_id | xero_core_xerotenant | tenant_id |
| xero_metadata_xerocontacts | organisation_id | xero_core_xerotenant | tenant_id |
| xero_metadata_xerotracking | organisation_id | xero_core_xerotenant | tenant_id |
| xero_sync_processtree | trigger_id | xero_sync_trigger | id |
| xero_sync_processtree_dependent_trees | from/to_processtree_id | xero_sync_processtree | id |
| xero_sync_processtree_sibling_trees | from/to_processtree_id | xero_sync_processtree | id |
| xero_sync_processtreeschedule | process_tree_id | xero_sync_processtree | id |
| xero_sync_trigger | process_tree_id | xero_sync_processtree | id |
| xero_sync_trigger | xero_last_update_id | xero_sync_xerolastupdate | id |
| xero_sync_xeroapicalllog | tenant_id | xero_core_xerotenant | tenant_id |
| xero_sync_xerolastupdate | organisation_id | xero_core_xerotenant | tenant_id |
| xero_sync_xerotaskexecutionlog | tenant_id | xero_core_xerotenant | tenant_id |
| xero_sync_xerotenantschedule | tenant_id | xero_core_xerotenant | tenant_id |
| xero_validation_* reports | organisation_id | xero_core_xerotenant | tenant_id |
| xero_validation_* lines | report_id | xero_validation_*report | id |
| xero_validation_* lines | account_id | xero_metadata_xeroaccount | account_id |
| xero_validation_* comparisons | report_id | xero_validation_*report | id |
| xero_validation_* comparisons | account_id | xero_metadata_xeroaccount | account_id |

---

## Django System Tables

| Table | Purpose |
|-------|---------|
| django_migrations | Migration history |
| django_content_type | Content type registry |
| django_admin_log | Admin audit log |
| django_session | Session storage |

---

*Generated from `klikk_financials_v4` database on 2026-03-13*
