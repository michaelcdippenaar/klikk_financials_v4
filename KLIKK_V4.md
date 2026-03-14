# Klikk Financials V4

Business intelligence platform built on **Django 5.1** (Python 3.10) that consolidates Xero accounting, IBM Planning Analytics (TM1), Investec banking, financial market data, and an AI agent into a single service.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Django Apps](#django-apps)
- [Database Schema & Relationships](#database-schema--relationships)
- [API Endpoints](#api-endpoints)
- [Key Services](#key-services)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Xero Integration (Detailed)](#xero-integration-detailed)
- [Investec Integration (Detailed)](#investec-integration-detailed)
- [Financial Investments (Detailed)](#financial-investments-detailed)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                     Klikk Financials V4                          │
│                  (Django / DRF / Gunicorn)                       │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────┐   │
│  │ Xero     │  │ Investec │  │ Planning  │  │ AI Agent     │   │
│  │ Suite    │  │          │  │ Analytics │  │ (RAG + Chat) │   │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘  └──────┬───────┘   │
│       │              │              │               │            │
│       ▼              ▼              ▼               ▼            │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │                     PostgreSQL                            │   │
│  │            (klikk_financials_v4 database)                 │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  External Services:                                              │
│  Xero API · TM1 REST API · Investec API · OpenAI · Serper       │
│  yfinance · Google Cloud (optional)                              │
└──────────────────────────────────────────────────────────────────┘
```

**Project root**: `klikk_business_intelligence/`
**Custom user model**: `user.User` (extends `AbstractUser`)
**Auth**: JWT (SimpleJWT) + Token + Session

---

## Django Apps

### User & Deployment

| App | Purpose |
|-----|---------|
| `user` | Custom user model (`User`: username, email, created_at, updated_at) |
| `deployment` | GitHub webhook receiver for automated deploys |

### Xero Suite

The Xero apps form a data pipeline that syncs accounting data from Xero, processes it into journals, and aggregates it into financial cubes.

| App | Purpose |
|-----|---------|
| `xero_auth` | OAuth2 flow with Xero — stores client credentials and tenant tokens |
| `xero_core` | Xero tenant registry — central reference for all multi-tenant data |
| `xero_metadata` | Account chart, tracking categories, contacts, business units |
| `xero_data` | Raw transaction sources, journals, and source documents |
| `xero_sync` | Sync state tracking and tenant scheduling |
| `xero_cube` | Aggregated trail balance, P&L by tracking, balance sheet |
| `xero_validation` | Reconciliation reports and data validation |
| `xero_integration` | Glue code connecting the Xero sub-apps |

### Financial & Banking

| App | Purpose |
|-----|---------|
| `investec` | Investec JSE share transactions, portfolio tracking, bank account sync |
| `financial_investments` | Market symbols, price history, dividends, splits, analyst data, news |

### Planning Analytics

| App | Purpose |
|-----|---------|
| `planning_analytics` | TM1 REST client, process execution, and end-to-end pipeline orchestration |

### AI Agent

| App | Purpose |
|-----|---------|
| `ai_agent` | RAG-powered chat agent with vectorised knowledge, TM1 proxy, and tool use |

---

## Database Schema & Relationships

### Xero Data Pipeline

```
xero_auth
  XeroClientCredentials ──► XeroTenantToken
                                  │
                                  ▼
xero_core
  XeroTenant ◄──────── (referenced by all Xero apps via FK "organisation")
      │
      ├──► xero_metadata
      │      XeroAccount (code, name, type, reporting_code)
      │      XeroTracking (option_id, name, option)
      │      XeroContacts (contacts_id, name)
      │      XeroBusinessUnits (division_code, business_unit_code)
      │
      ├──► xero_data
      │      XeroTransactionSource (transactions_id, transaction_source, contact FK)
      │      XeroDocument (file_name, file → TransactionSource FK)
      │      XeroJournalsSource (journal_id, journal_type, processed)
      │      XeroJournals
      │        ├── account → XeroAccount
      │        ├── contact → XeroContacts
      │        ├── tracking1, tracking2 → XeroTracking
      │        ├── transaction_source → XeroTransactionSource
      │        ├── journal_source → XeroJournalsSource
      │        └── date, amount, debit, credit
      │
      ├──► xero_cube
      │      XeroTrailBalance
      │        ├── account → XeroAccount
      │        ├── contact → XeroContacts
      │        ├── tracking1, tracking2 → XeroTracking
      │        └── year, month, fin_year, fin_period, amount, debit, credit
      │      XeroPnlByTracking (similar FKs)
      │      XeroBalanceSheet (similar FKs)
      │
      ├──► xero_sync
      │      XeroLastUpdate (end_point, date)
      │      XeroTenantSchedule (enabled, update_interval_minutes)
      │
      └──► xero_validation
             XeroTrailBalanceReport → XeroTrailBalanceReportLine (account FK)
```

### Investec & Financial Investments

```
investec
  InvestecJseShareNameMapping ◄──OneToOne── financial_investments.Symbol
  InvestecJseTransaction
  InvestecJsePortfolio
  InvestecJseShareMonthlyPerformance
  InvestecBankAccount ──► InvestecBankTransaction
  InvestecBankSyncLog

financial_investments
  Symbol ──► PricePoint, Dividend, Split, SymbolInfo
           ──► FinancialStatement, EarningsReport, EarningsEstimate
           ──► AnalystRecommendation, AnalystPriceTarget
           ──► OwnershipSnapshot, NewsItem
  WatchlistTablePreference
```

### AI Agent

```
ai_agent
  AgentProject ──► AgentSession ──► AgentMessage
                                ──► AgentToolExecutionLog
                                ──► AgentApprovalRequest
               ──► SystemDocument ──► KnowledgeCorpus
               ──► KnowledgeChunkEmbedding (vectorised chunks)

  KnowledgeCorpus ──► SystemDocument
                  ──► KnowledgeChunkEmbedding
  GlossaryRefreshRequest
```

---

## API Endpoints

### Authentication — `api/auth/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `register/` | Create a new user |
| POST | `login/` | Obtain JWT token pair |
| POST | `refresh/` | Refresh JWT access token |
| POST | `token/` | Alternative token obtain |
| POST | `token/refresh/` | Alternative token refresh |
| POST | `token/verify/` | Verify token validity |
| GET | `nginx-check/` | Health check for Nginx auth |

### Xero Auth — `xero/auth/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `initiate/` | Start OAuth2 flow |
| GET | `callback/` | OAuth2 redirect handler |
| GET | `status/` | Connection status |
| CRUD | `credentials/` | Manage client credentials |

### Xero Core — `xero/core/`

| Method | Path | Description |
|--------|------|-------------|
| GET | `tenants/` | List connected Xero tenants |

### Xero Metadata — `xero/metadata/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `update/` | Sync metadata from Xero API |
| GET | `accounts/search/` | Search account chart |

### Xero Sync — `xero/sync/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `update/` | Trigger sync from Xero |
| GET | `api-call-stats/` | API call statistics |

### Xero Data — `xero/data/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `update/journals/` | Fetch journals from Xero |
| POST | `process/journals/` | Process raw journal data |
| POST | `sync/documents/` | Sync source documents |
| GET | `documents/by-transaction/<id>/` | Documents for a transaction |

### Xero Cube — `xero/cube/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `process/` | Build cube from journals |
| GET | `summary/` | Cube summary stats |
| GET | `trail-balance/` | Trail balance data |
| GET | `line-items/` | Detailed line items |
| POST | `import-pnl-by-tracking/` | Import P&L by tracking |
| GET | `pnl-summary/` | P&L summary |
| GET | `account-summary/` | Account-level summary |

### Xero Validation — `xero/validation/`

| Method | Path | Description |
|--------|------|-------------|
| GET | `balance-sheet/` | Balance sheet report |
| GET | `reconcile/` | Reconciliation check |
| POST | `import-profit-loss/` | Import P&L from Xero |
| GET | `compare-profit-loss/` | Compare P&L periods |
| GET | `export-trail-balance/` | Export trail balance (Excel) |
| GET | `export-profit-loss/` | Export P&L (Excel) |

### Investec — `api/investec/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `upload/` | Upload JSE transactions |
| GET | `transactions/` | List JSE transactions |
| POST | `portfolio/upload/` | Upload portfolio data |
| CRUD | `mapping/` | Share name mappings |
| GET | `mapping/unmapped-share-names/` | Unmapped share names |
| POST | `mapping/upload/` | Bulk upload mappings |
| GET | `export/mapping/` | Export mappings |
| GET | `export/companies/` | Export companies |
| GET | `export/share-names/` | Export share names |
| GET | `export/transactions/` | Export transactions |
| GET | `bank/accounts/` | Bank accounts |
| GET | `bank/transactions/` | Bank transactions |
| GET | `bank/transactions/export/` | Export bank transactions |
| GET | `bank/sync-status/` | Bank sync status |
| POST | `bank/sync/` | Trigger bank sync |

### Financial Investments — `api/financial-investments/`

| Method | Path | Description |
|--------|------|-------------|
| GET | `symbols/` | List tracked symbols |
| GET | `symbols/<symbol>/` | Symbol detail |
| GET | `symbols/<symbol>/history/` | Price history |
| POST | `symbols/<symbol>/refresh/` | Refresh symbol data |
| POST | `symbols/<symbol>/refresh-extra/` | Refresh extra data |
| GET | `symbols/<symbol>/dividends/` | Dividend history |
| GET | `symbols/<symbol>/splits/` | Stock splits |
| GET | `symbols/<symbol>/info/` | Symbol info |
| GET | `symbols/<symbol>/financial-statements/` | Financial statements |
| GET | `symbols/<symbol>/earnings/` | Earnings reports |
| GET | `symbols/<symbol>/earnings-estimate/` | Earnings estimates |
| GET | `symbols/<symbol>/analyst-recommendations/` | Analyst recommendations |
| GET | `symbols/<symbol>/analyst-price-target/` | Analyst price targets |
| GET | `symbols/<symbol>/ownership/` | Ownership snapshots |
| GET | `symbols/<symbol>/news/` | News items |
| POST | `watchlist-preference/save/` | Save watchlist preferences |
| GET | `watchlist-preference/` | Get watchlist preferences |

### Planning Analytics — `api/planning-analytics/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `pipeline/run/` | Run full pipeline |
| POST | `tm1/execute/` | Execute TM1 process |
| POST | `tm1/test-connection/` | Test TM1 connection |
| GET | `tm1/config/` | TM1 configuration |
| GET | `tm1/processes/` | List TM1 processes |

### AI Agent — `api/ai-agent/`

| Method | Path | Description |
|--------|------|-------------|
| GET | `health/` | Agent health check |
| GET | `status/` | Agent status |
| POST | `glossary/refresh/` | Refresh glossary |
| CRUD | `projects/` | Manage projects |
| GET | `projects/<id>/` | Project detail |
| POST | `projects/<id>/import-tm1-docs/` | Import TM1 docs into project |
| CRUD | `corpora/` | Knowledge corpora |
| POST | `corpora/<id>/vectorize/` | Vectorise corpus documents |
| POST | `corpora/<id>/search/` | Semantic search over corpus |
| CRUD | `sessions/` | Chat sessions |
| GET | `sessions/<id>/messages/` | Session messages |
| GET | `sessions/<id>/memory/` | Session memory |
| POST | `sessions/<id>/import-cursor-chat/` | Import Cursor chat transcript |
| POST | `sessions/<id>/export-to-system-doc/` | Export session to system doc |
| POST | `sessions/<id>/run/` | Run agent (no tools) |
| POST | `sessions/<id>/run-with-tools/` | Run agent with tool use |
| GET | `sessions/<id>/executions/` | Tool execution history |
| CRUD | `system-docs/` | System documents |
| POST | `system-docs/generate/` | Auto-generate system docs |
| GET | `system-docs/<id>/` | System doc detail |
| GET | `tm1/config/` | TM1 config for agent |
| POST | `tm1/proxy/` | TM1 REST proxy |
| POST | `tm1/test-connection/` | Test TM1 from agent |
| GET | `tm1/version/` | TM1 server version |

### Deployment — `deployment/`

| Method | Path | Description |
|--------|------|-------------|
| POST | `webhook/github/` | GitHub webhook for auto-deploy |

---

## Key Services

### AI Agent Services

| File | Purpose |
|------|---------|
| `vector_store.py` | Document chunking, OpenAI embeddings, `vectorize_corpus_documents`, `semantic_search_chunks` — powers RAG retrieval |
| `chat_runner.py` | Context assembly, OpenAI/Gemini chat completion, tool orchestration (`tm1_get`, `tm1_mdx`, `web_search`), `generate_assistant_reply_with_tools` |
| `tm1_proxy.py` | Safe TM1 REST proxy — blocks writes to protected cube versions (Actuals, Forecast) |
| `tm1_docs.py` | Fetches TM1 cubes, dimensions, processes, rules; builds documentation bundles |
| `system_doc_builder.py` | Auto-generates system documentation from Django URLs, TM1 metadata, and models |
| `cursor_chat_import.py` | Imports Cursor IDE chat transcripts from `~/.cursor/projects/` |
| `session_transcript.py` | Builds markdown transcripts of agent sessions for export |

### Planning Analytics Services

| File | Purpose |
|------|---------|
| `tm1_client.py` | Low-level TM1 REST client: `execute_process`, `test_connection` |
| `pipeline.py` | End-to-end orchestration: update metadata → update Postgres → process journals → run TM1 processes |

### Xero Services

| File | Purpose |
|------|---------|
| `update_metadata` | Sync accounts, contacts, tracking categories from Xero API |
| `update_financial_data` | Sync transactions from Xero, update journal entries |
| `process_xero_data` | Process journals into trail balance, optional P&L YTD |

---

## Configuration

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `DJANGO_SETTINGS_MODULE` | Settings module path |
| `DJANGO_SECRET_KEY` | Django secret key |
| `ALLOWED_HOSTS` | Allowed host names |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | PostgreSQL connection |
| `TM1_ADDRESS`, `TM1_PORT`, `TM1_USER`, `TM1_PASSWORD` | TM1 REST API connection |
| `TM1_SSL`, `TM1_VERIFY_SSL`, `TM1_REQUEST_TIMEOUT` | TM1 SSL and timeout settings |
| `INVESTEC_CLIENT_ID`, `INVESTEC_CLIENT_SECRET`, `INVESTEC_API_KEY` | Investec API credentials |
| `XERO_DOCUMENTS_ROOT` | Local path for Xero documents |
| `AI_AGENT_OPENAI_API_KEY` | OpenAI API key for agent |
| `AI_AGENT_MODEL` | LLM model name (e.g. gpt-4o) |
| `AI_AGENT_EMBEDDING_MODEL` | Embedding model name |
| `SERPER_API_KEY` | Serper web search API key |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP service account JSON path |
| `GITHUB_WEBHOOK_SECRET` | GitHub webhook signing secret |

### External Services

| Service | Used For |
|---------|----------|
| **PostgreSQL** | Primary database |
| **Xero** | Accounting data (OAuth2) |
| **IBM TM1** | Planning Analytics REST API |
| **Investec** | Private banking and JSE data |
| **OpenAI** | Chat completions and embeddings |
| **Serper** | Web search for agent |
| **yfinance** | Market data for financial investments |
| **Google Cloud** | Optional BigQuery exports |

---

## Deployment

| Component | Location | Details |
|-----------|----------|---------|
| **Dockerfile** | `/Dockerfile` | Python 3.10-slim, installs psycopg2 and openpyxl, exposes port 8001 |
| **docker-compose.yml** | `/docker-compose.yml` | Single `web` service, staging settings, `host-gateway` for DB access, volumes for media and static |
| **Entrypoint** | `/scripts/docker-entrypoint.sh` | Runs migrate, collectstatic, then Gunicorn with 3 workers on port 8001 (timeout 3600s) |
| **Deploy script** | `/scripts/server/deploy.sh` | Used by GitHub webhook — pull, migrate, restart |

### Running Locally

```bash
# Activate virtual environment
source venv/bin/activate

# Apply migrations
python manage.py migrate

# Run development server
python manage.py runserver 0.0.0.0:8001
```

### Running with Docker

```bash
docker-compose up --build
```

---

## Xero Integration (Detailed)

The Xero suite is a multi-app pipeline that connects to the Xero accounting API via OAuth2, syncs financial data incrementally, processes it into double-entry journals, and aggregates those journals into a trail balance cube — with optional export to Google BigQuery and IBM TM1.

### OAuth2 Flow (`xero_auth`)

```
Frontend                    Backend                         Xero
   │                           │                              │
   │ ── GET /xero/auth/initiate/ ──►                          │
   │                           │ ── build auth_url ──────────►│
   │ ◄── redirect to Xero ────│                              │
   │                           │                              │
   │ (user authorises)         │                              │
   │                           │ ◄── GET /xero/callback?code= │
   │                           │ ── POST token exchange ─────►│
   │                           │ ◄── access_token + refresh ──│
   │                           │ ── GET connections ─────────►│
   │                           │ ◄── tenant list ─────────────│
   │                           │                              │
   │                           │ save XeroTenant              │
   │                           │ save XeroTenantToken         │
   │                           │ save tenant_tokens JSON      │
   │ ◄── redirect to frontend  │                              │
```

**Token refresh**: Automatic on every API call. `XeroApiClient` checks if the token expires within 30 seconds; if so, it POSTs to `refresh_url` with `grant_type=refresh_token` using Basic auth (`client_id:client_secret`).

**Scopes**: `openid`, `profile`, `email`, `offline_access`, `accounting.transactions`, `accounting.transactions.read`, `accounting.reports.read`, `accounting.journals.read`, `accounting.settings`, `accounting.settings.read`, `accounting.contacts`, `accounting.contacts.read`, `accounting.attachments`, `accounting.attachments.read`.

#### Key Models

**XeroClientCredentials**

| Field | Type | Description |
|-------|------|-------------|
| `user` | FK → User | Credential owner |
| `client_id` | CharField(100) | Xero OAuth2 client ID |
| `client_secret` | CharField(100) | Xero OAuth2 client secret |
| `scope` | JSONField | OAuth2 scopes |
| `tenant_tokens` | JSONField | Per-tenant token store: `{tenant_id: {token, refresh_token, expires_at}}` |
| `active` | BooleanField | Whether active |

**XeroTenantToken**

| Field | Type | Description |
|-------|------|-------------|
| `tenant` | FK → XeroTenant | Linked tenant |
| `credentials` | FK → XeroClientCredentials | Parent credentials |
| `token` | JSONField | Access token payload |
| `refresh_token` | CharField(1000) | Refresh token |
| `expires_at` | DateTimeField | Expiry timestamp |

### Tenants (`xero_core`)

**XeroTenant** — Central reference for all multi-tenant Xero data.

| Field | Type | Description |
|-------|------|-------------|
| `tenant_id` | CharField(100), PK | Xero tenant UUID |
| `tenant_name` | CharField(100) | Display name |
| `tracking_category_1_id` | CharField(64) | Xero TrackingCategoryID for slot 1 |
| `tracking_category_2_id` | CharField(64) | Xero TrackingCategoryID for slot 2 |
| `fiscal_year_start_month` | IntegerField(1–12) | Fiscal year start month (from Xero Organisation, default 7) |

Every other Xero model has an `organisation` FK pointing back to `XeroTenant`.

### Metadata Sync (`xero_metadata`)

Triggered via `POST /xero/metadata/update/`. Syncs in sequence:

1. **Organisation** → updates `fiscal_year_start_month`
2. **Accounts** → `XeroAccount` (chart of accounts with codes, types, reporting codes)
3. **Tracking Categories** → `XeroTracking` (tracking options, assigns slot 1/2 IDs on tenant)
4. **Contacts** → `XeroContacts`

All calls use `if_modified_since` from `XeroLastUpdate` for incremental sync.

#### Key Models

**XeroAccount**

| Field | Type | Description |
|-------|------|-------------|
| `account_id` | CharField(40), PK | Xero AccountID |
| `organisation` | FK → XeroTenant | Tenant |
| `business_unit` | FK → XeroBusinessUnits | Derived from first 2 chars of account code |
| `code` | CharField(10) | Account code |
| `name` | CharField(150) | Account name |
| `type` | CharField(30) | Account type (REVENUE, EXPENSE, BANK, etc.) |
| `grouping` | CharField(30) | Class (ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE) |
| `reporting_code` | TextField | Xero ReportingCode |
| `reporting_code_name` | TextField | Xero ReportingCodeName |

**XeroTracking**

| Field | Type | Description |
|-------|------|-------------|
| `option_id` | TextField(1024) | Xero TrackingOptionID |
| `organisation` | FK → XeroTenant | Tenant |
| `name` | TextField | Category name (e.g. "Region") |
| `option` | TextField | Option name (e.g. "Gauteng") |
| `tracking_category_id` | CharField(64) | Parent TrackingCategoryID |

**XeroContacts**

| Field | Type | Description |
|-------|------|-------------|
| `contacts_id` | CharField(55), PK | Xero ContactID |
| `organisation` | FK → XeroTenant | Tenant |
| `name` | TextField | Contact name |

**XeroBusinessUnits**

| Field | Type | Description |
|-------|------|-------------|
| `organisation` | FK → XeroTenant | Tenant |
| `division_code` | CharField(1) | Division code |
| `business_unit_code` | CharField(1) | Business unit code |
| `division_description` | CharField(100) | Division label |
| `business_unit_description` | CharField(100) | Business unit label |

### Transaction & Journal Sync (`xero_data`)

#### Fetch Flow

Triggered via `POST /xero/data/update/journals/`. Fetches from Xero API (paginated, incremental via `if_modified_since`):

- Bank transactions, Invoices, Payments, Credit notes
- Prepayments, Overpayments, Purchase orders, Bank transfers, Expense claims
- Manual journals

Each record is stored in either `XeroTransactionSource` (transaction types) or `XeroJournalsSource` (journals).

#### Processing Flow

Triggered via `POST /xero/data/process/journals/`. Two paths:

**Transaction → Journal conversion** (`transaction_processor`):
- Invoices, bank transactions, payments, credit notes, prepayments, overpayments → journal lines
- Uses system accounts (AR, AP, tax, bank) and line-level tracking
- Creates `XeroJournals` entries

**Manual Journal → Journal conversion** (`XeroJournalsSource.objects.create_journals_from_xero`):
- Filters `processed=False` sources
- Skips VOIDED, DELETED, DRAFT
- For each line: resolves account, contact, tracking1, tracking2
- Inherits tracking from parent transaction source when missing on line
- Marks sources as `processed=True`

#### Key Models

**XeroTransactionSource**

| Field | Type | Description |
|-------|------|-------------|
| `organisation` | FK → XeroTenant | Tenant |
| `transactions_id` | CharField(51) | Xero transaction ID |
| `transaction_source` | CharField(51) | Type: BankTransaction, Invoice, Payment, CreditNote, etc. |
| `contact` | FK → XeroContacts | Contact |
| `collection` | JSONField | Raw Xero payload |

**XeroJournalsSource**

| Field | Type | Description |
|-------|------|-------------|
| `organisation` | FK → XeroTenant | Tenant |
| `journal_id` | CharField(51) | Xero JournalID |
| `journal_number` | IntegerField | Journal number |
| `journal_type` | CharField(20) | `journal` or `manual_journal` |
| `collection` | JSONField | Raw payload |
| `processed` | BooleanField | Whether processed into XeroJournals |

**XeroJournals** — The normalised double-entry journal.

| Field | Type | Description |
|-------|------|-------------|
| `journal_id` | CharField(200) | Line ID |
| `journal_number` | IntegerField | Journal number |
| `journal_type` | CharField(20) | `journal` or `manual_journal` |
| `organisation` | FK → XeroTenant | Tenant |
| `account` | FK → XeroAccount | Account |
| `contact` | FK → XeroContacts | Contact |
| `transaction_source` | FK → XeroTransactionSource | Source transaction |
| `journal_source` | FK → XeroJournalsSource | Source journal |
| `tracking1` | FK → XeroTracking | Tracking slot 1 |
| `tracking2` | FK → XeroTracking | Tracking slot 2 |
| `date` | DateTimeField | Transaction date |
| `description` | TextField | Line description |
| `reference` | TextField | Reference |
| `amount` | DecimalField(30,2) | Net amount |
| `debit` | DecimalField(30,2) | Positive component |
| `credit` | DecimalField(30,2) | Negative component |
| `tax_amount` | DecimalField(30,2) | Tax amount |

**XeroDocument**

| Field | Type | Description |
|-------|------|-------------|
| `organisation` | FK → XeroTenant | Tenant |
| `transaction_source` | FK → XeroTransactionSource | Parent transaction |
| `file_name` | CharField(255) | File name |
| `file` | FileField | Stored file |
| `content_type` | CharField(128) | MIME type |
| `xero_attachment_id` | CharField(64) | Xero AttachmentID |

### Trail Balance Cube (`xero_cube`)

Triggered via `POST /xero/cube/process/`. Steps:

1. **Process journals** — runs journal processing from `xero_data`
2. **Consolidate journals** — SQL aggregation into `XeroTrailBalance`
3. **Balance-to-date** — window function for cumulative ASSET/LIABILITY/EQUITY balances
4. **BigQuery export** — optional export to `Xero.TrailBalance_Movement_V2_{tenant_id}`

#### Consolidation SQL

```sql
INSERT INTO xero_cube_xerotrailbalance
    (organisation_id, account_id, date, year, month,
     fin_year, fin_period, contact_id,
     tracking1_id, tracking2_id,
     amount, debit, credit, tax_amount, balance_to_date)
SELECT
    j.organisation_id,
    j.account_id,
    make_date(EXTRACT(YEAR FROM j.date)::int,
              EXTRACT(MONTH FROM j.date)::int, 1),
    EXTRACT(YEAR FROM j.date)::int,
    EXTRACT(MONTH FROM j.date)::int,
    -- Fiscal year: if month >= fiscal_start → current year, else year-1
    CASE WHEN EXTRACT(MONTH FROM j.date) >= fiscal_start
         THEN EXTRACT(YEAR FROM j.date)::int
         ELSE EXTRACT(YEAR FROM j.date)::int - 1 END,
    -- Fiscal period: month offset from fiscal_start
    CASE WHEN EXTRACT(MONTH FROM j.date) >= fiscal_start
         THEN EXTRACT(MONTH FROM j.date)::int - fiscal_start + 1
         ELSE EXTRACT(MONTH FROM j.date)::int + (12 - fiscal_start) + 1 END,
    COALESCE(j.contact_id, ts.contact_id),
    j.tracking1_id,
    j.tracking2_id,
    SUM(j.amount),
    SUM(CASE WHEN j.amount > 0 THEN j.amount ELSE 0 END),
    SUM(CASE WHEN j.amount < 0 THEN j.amount ELSE 0 END),
    SUM(j.tax_amount),
    NULL
FROM xero_data_xerojournals j
LEFT JOIN xero_data_xerotransactionsource ts
    ON j.transaction_source_id = ts.transactions_id
WHERE j.organisation_id = %s
GROUP BY j.organisation_id, j.account_id,
         EXTRACT(YEAR FROM j.date), EXTRACT(MONTH FROM j.date),
         COALESCE(j.contact_id, ts.contact_id),
         j.tracking1_id, j.tracking2_id
HAVING SUM(j.amount) != 0
```

Contact falls back to the transaction source contact when the journal line has none: `COALESCE(j.contact_id, ts.contact_id)`.

#### Key Models

**XeroTrailBalance**

| Field | Type | Description |
|-------|------|-------------|
| `organisation` | FK → XeroTenant | Tenant |
| `account` | FK → XeroAccount | Account |
| `date` | DateField | First day of month |
| `year`, `month` | IntegerField | Calendar period |
| `fin_year`, `fin_period` | IntegerField | Fiscal year and period |
| `contact` | FK → XeroContacts | Contact |
| `tracking1`, `tracking2` | FK → XeroTracking | Tracking options |
| `amount` | DecimalField(30,2) | Net movement |
| `debit`, `credit` | DecimalField(30,2) | Debit/credit components |
| `tax_amount` | DecimalField(30,2) | Tax total |
| `balance_to_date` | DecimalField(30,2) | YTD balance (for BS accounts) |

**XeroPnlByTracking** — Imported Xero P&L report amounts by tracking option.

| Field | Type | Description |
|-------|------|-------------|
| `organisation` | FK → XeroTenant | Tenant |
| `tracking` | FK → XeroTracking | Tracking option (null = overall) |
| `account` | FK → XeroAccount | Account |
| `year`, `month` | IntegerField | Period |
| `xero_amount` | DecimalField(30,2) | Amount from Xero P&L |

**XeroBalanceSheet** — Cumulative balance sheet built from trail balance.

| Field | Type | Description |
|-------|------|-------------|
| `organisation` | FK → XeroTenant | Tenant |
| `date` | DateField | Period date |
| `year`, `month` | IntegerField | Period |
| `account` | FK → XeroAccount | Account |
| `contact` | FK → XeroContacts | Contact |
| `amount` | DecimalField(30,2) | Period movement |
| `balance` | DecimalField(30,2) | Cumulative balance |

### Sync Scheduling (`xero_sync`)

**XeroLastUpdate** — Tracks the last successful sync per endpoint per tenant.

| Field | Type | Description |
|-------|------|-------------|
| `end_point` | CharField | Endpoint name (accounts, contacts, journals, etc.) |
| `organisation` | FK → XeroTenant | Tenant |
| `date` | DateTimeField | Last sync timestamp |

**XeroTenantSchedule** — Per-tenant scheduling configuration.

| Field | Type | Description |
|-------|------|-------------|
| `tenant` | OneToOne → XeroTenant | Tenant |
| `enabled` | BooleanField | Whether scheduling is active |
| `update_interval_minutes` | IntegerField | Minutes between runs (default 60) |
| `update_start_time` | TimeField | Preferred daily start time |
| `last_update_run` | DateTimeField | Last update run |
| `last_process_run` | DateTimeField | Last process run |
| `next_update_run` | DateTimeField | Next scheduled run |

Scheduling is driven by `Trigger`/`ProcessTree` or external jobs — there is no built-in cron.

### Validation (`xero_validation`)

Compares data in the database against Xero's own reports to detect discrepancies:

- **Trail balance**: Import Xero trial balance report → compare to `XeroTrailBalance` → `TrailBalanceComparison` (match, mismatch, missing_in_db, missing_in_xero)
- **P&L**: Import Xero P&L report → compare per-period amounts → `ProfitAndLossComparison`
- **Reconciliation**: Full financial year reconciliation of P&L + Balance Sheet vs trail balance

### BigQuery Export (`xero_integration`)

Exports to Google BigQuery tables:
- `Xero.TrailBalance_Movement_V2_{tenant_id}`
- `Xero.BalanceSheet_Balance_{tenant_id}`
- `Xero.Accounts_{tenant_id}`

Requires `GOOGLE_APPLICATION_CREDENTIALS`. Called by `xero_cube` after processing.

---

## Investec Integration (Detailed)

The Investec integration covers two separate domains: JSE share trading (via Excel uploads) and private banking (via Investec Open API).

### JSE Share Trading

#### Transaction Upload Flow

```
Excel file (TransactionHistory-All-YYYYMMDD-YYYYMMDD.xlsx)
  │
  ▼
POST /api/investec/upload/
  │
  ├── Detect header row, map columns
  ├── Extract date range from filename
  ├── Delete existing transactions in range
  ├── Parse rows:
  │     ├── Extract share_name from description (e.g. "DIV. 327 NINETY 1L" → NINETY)
  │     ├── Infer type (Buy, Sell, Dividend, Fee, Broker Fee)
  │     └── Extract value_per_share from "at X Cents" in description
  ├── Bulk create InvestecJseTransaction
  └── Calculate TTM dividends → InvestecJseShareMonthlyPerformance
```

#### Portfolio Upload Flow

```
Excel file (Holdings-YYYYMMDD.xlsx)
  │
  ▼
POST /api/investec/portfolio/upload/
  │
  ├── Extract date from filename
  ├── Parse "COMPANY NAME (CODE)" from Instrument Description
  ├── Delete existing rows for that month
  ├── Bulk create InvestecJsePortfolio
  └── Update InvestecJseShareNameMapping (share_code → company)
```

#### Share Name Mapping

Maps transaction share names to portfolio company names and share codes. Three name fields allow for aliases:

| Source | Creates/Updates Mapping |
|--------|------------------------|
| Portfolio upload | Sets `company` and `share_code` for matching entries |
| Mapping Excel upload | Bulk sets `share_name`, `share_name2`, `share_name3`, `company`, `share_code` |
| Manual | Via API or admin |

**Unmapped detection**: `GET /api/investec/mapping/unmapped-share-names/` returns transaction share names not found in any mapping's `share_name`, `share_name2`, or `share_name3`.

#### TTM Dividend Calculation

After transaction upload, the system:
1. Groups dividend transactions by `(share_name, dividend_type)`
2. Resamples to month-end
3. Computes rolling 12-month sum
4. Creates/updates `InvestecJseShareMonthlyPerformance` with `dividend_ttm`, `closing_price`, `quantity`, `dividend_yield`

#### Key Models

**InvestecJseTransaction**

| Field | Type | Description |
|-------|------|-------------|
| `date` | DateField | Transaction date |
| `year`, `month`, `day` | IntegerField | Derived from date |
| `account_number` | CharField(50) | Account number |
| `description` | CharField(255) | Full description |
| `share_name` | CharField(100) | Share name (can be empty for account-level transactions) |
| `type` | CharField(50) | Buy, Sell, Dividend, Fee, Broker Fee, Special Dividend, etc. |
| `quantity` | DecimalField(15,4) | Quantity |
| `value` | DecimalField(15,2) | Value |
| `value_per_share` | DecimalField(15,2) | Price per share (Buy/Sell) |
| `value_calculated` | DecimalField(15,2) | value_per_share × quantity (negative for Buy) |
| `dividend_ttm` | DecimalField(15,2) | Trailing 12-month dividend sum |

**InvestecJsePortfolio**

| Field | Type | Description |
|-------|------|-------------|
| `date` | DateField | Portfolio date |
| `year`, `month`, `day` | IntegerField | Derived from date |
| `company` | CharField(100) | Company name |
| `share_code` | CharField(20) | Share code (e.g. NED) |
| `quantity` | DecimalField(15,4) | Quantity held |
| `currency` | CharField(10) | Currency (default ZAR) |
| `unit_cost` | DecimalField(15,4) | Cost per unit |
| `total_cost` | DecimalField(15,2) | Total cost |
| `price` | DecimalField(15,4) | Current market price |
| `total_value` | DecimalField(15,2) | Market value |
| `exchange_rate` | DecimalField(15,6) | Exchange rate (optional) |
| `move_percent` | DecimalField(10,4) | Price movement % |
| `portfolio_percent` | DecimalField(10,4) | Portfolio weight % |
| `profit_loss` | DecimalField(15,2) | Profit/loss |
| `annual_income_zar` | DecimalField(15,2) | Annual income in ZAR |

**InvestecJseShareNameMapping**

| Field | Type | Description |
|-------|------|-------------|
| `share_name` | CharField(100), unique | Primary share name |
| `share_name2` | CharField(100) | Alias 2 |
| `share_name3` | CharField(100) | Alias 3 |
| `company` | CharField(100) | Company name |
| `share_code` | CharField(20), unique | Share code |

**InvestecJseShareMonthlyPerformance**

| Field | Type | Description |
|-------|------|-------------|
| `share_name` | CharField(100) | Share name |
| `date` | DateField | Month-end date |
| `year`, `month` | IntegerField | Period |
| `dividend_type` | CharField(50) | Dividend, Special Dividend, Foreign Dividend, Dividend Tax |
| `investec_account` | CharField(50) | Account number |
| `dividend_ttm` | DecimalField(15,2) | Trailing 12-month dividend sum |
| `closing_price` | DecimalField(15,2) | Month-end price |
| `quantity` | DecimalField(15,4) | Month-end quantity |
| `total_market_value` | DecimalField(15,2) | quantity × price |
| `dividend_yield` | DecimalField(10,4) | dividend_ttm / total_market_value |

Unique constraint: `(share_name, date, dividend_type)`.

### Private Banking (Investec Open API)

#### Authentication

- **Method**: OAuth2 client_credentials flow
- **Auth header**: Basic auth with `client_id:client_secret`
- **API key**: Sent as `x-api-key` header
- **Token caching**: Cached per `client_id`, refreshed before expiry
- **Multiple profiles**: Supports `INVESTEC_CLIENT_ID`, `INVESTEC_CLIENT_ID_2`, etc. for multiple Investec accounts

#### Bank Sync Flow

```
POST /api/investec/bank/sync/
  │
  ▼
For each INVESTEC_PROFILES entry:
  │
  ├── Get OAuth2 access token
  ├── GET /za/pb/v1/accounts → list accounts
  │
  └── For each account:
        ├── Upsert InvestecBankAccount
        ├── GET /za/pb/v1/accounts/{id}/transactions (month-by-month)
        └── Upsert InvestecBankTransaction
              ├── Match by uuid (primary)
              ├── Match by fallback_key (when no uuid)
              └── Match by (account, posting_date, posted_order)

Update InvestecBankSyncLog.last_synced_at
```

Incremental: uses `last_synced_at` as `from_date`; if never synced, uses last 180 days.

#### Key Models

**InvestecBankAccount**

| Field | Type | Description |
|-------|------|-------------|
| `account_id` | CharField(40) | Investec API account ID |
| `account_number` | CharField(40) | Account number |
| `account_name` | CharField(70) | Account name |
| `reference_name` | CharField(70) | Reference name |
| `product_name` | CharField(70) | Product name (e.g. Private Bank Account) |
| `kyc_compliant` | BooleanField | KYC compliance status |
| `profile_id` | CharField(70) | Profile ID |
| `profile_name` | CharField(70) | Profile name |

**InvestecBankTransaction**

| Field | Type | Description |
|-------|------|-------------|
| `account` | FK → InvestecBankAccount | Parent account |
| `type` | CharField(10) | CREDIT or DEBIT |
| `transaction_type` | CharField(40) | Type classification |
| `status` | CharField(10) | POSTED or PENDING |
| `description` | CharField(255) | Description |
| `card_number` | CharField(40) | Card number (if applicable) |
| `posted_order` | IntegerField | Posting order |
| `posting_date` | DateField | Posting date |
| `value_date` | DateField | Value date |
| `action_date` | DateField | Action date |
| `transaction_date` | DateField | Transaction date |
| `amount` | DecimalField(15,2) | Amount |
| `running_balance` | DecimalField(15,2) | Running balance |
| `uuid` | CharField(40) | Investec transaction UUID |
| `fallback_key` | CharField(64) | Hash fallback when no uuid/posted_order |

Unique constraint: `(account, posting_date, posted_order)` when both are set.

**InvestecBankSyncLog**

| Field | Type | Description |
|-------|------|-------------|
| `key` | CharField(32) | Unique key (default: 'default') |
| `last_synced_at` | DateTimeField | Last sync timestamp |

### Link to Financial Investments

`financial_investments.Symbol` has an optional `OneToOneField` to `InvestecJseShareNameMapping`:

```
Symbol (e.g. NED.JO)
  └── share_name_mapping → InvestecJseShareNameMapping
        ├── share_name: "NEDBANK"
        ├── company: "Nedbank Group Limited"
        └── share_code: "NED"
```

This links market data (price history, dividends, analyst data from yfinance) to Investec JSE trading records. The link is set manually.

---

## Financial Investments (Detailed)

Market data tracking powered by **yfinance**. Supports equities, ETFs, indices, and forex.

### Symbol Refresh Flow

**Price history** — `POST /api/financial-investments/symbols/<symbol>/refresh/`:
1. Fetches 2 years of OHLCV data via `yf.Ticker(symbol).history()`
2. Deletes existing `PricePoint` in range
3. Bulk creates new `PricePoint` records
4. Optionally updates `Symbol.name` and `Symbol.exchange` from `ticker.info`

**Extra data** — `POST /api/financial-investments/symbols/<symbol>/refresh-extra/`:
- Body: `{ "types": ["dividends", "splits", "company_info", "financial_statements", "earnings", "earnings_estimate", "analyst_recommendations", "analyst_price_target", "ownership", "news"] }`
- Omitting `types` refreshes all

### Key Models

**Symbol**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | CharField(20), unique | Ticker (e.g. NED.JO) |
| `name` | CharField(255) | Company name |
| `exchange` | CharField(50) | Exchange |
| `category` | CharField(20) | equity, etf, index, forex |
| `share_name_mapping` | OneToOne → InvestecJseShareNameMapping | Optional Investec link |

**PricePoint** — Daily OHLCV data.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | FK → Symbol | Symbol |
| `date` | DateField | Date |
| `open`, `high`, `low`, `close` | DecimalField(18,4) | OHLC prices |
| `volume` | BigIntegerField | Volume |
| `adjusted_close` | DecimalField(18,4) | Adjusted close |

**Dividend**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | FK → Symbol | Symbol |
| `date` | DateField | Ex-date |
| `amount` | DecimalField(18,6) | Dividend amount |
| `currency` | CharField(10) | Currency |

**Split**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | FK → Symbol | Symbol |
| `date` | DateField | Split date |
| `ratio` | DecimalField(12,4) | Split ratio (2.0 = 2-for-1) |

**SymbolInfo** — Cached `ticker.info` payload.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | OneToOne → Symbol | Symbol |
| `data` | JSONField | Full info payload |
| `fetched_at` | DateTimeField | Last fetch |

**FinancialStatement** — Income statement, balance sheet, cash flow.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | FK → Symbol | Symbol |
| `statement_type` | CharField(20) | income_stmt, balance_sheet, cash_flow |
| `period_end` | DateField | Period end |
| `freq` | CharField(20) | yearly, quarterly, trailing |
| `data` | JSONField | Statement data |

**EarningsReport**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | FK → Symbol | Symbol |
| `period_end` | DateField | Period end |
| `freq` | CharField(20) | Frequency |
| `data` | JSONField | Earnings data |

**EarningsEstimate** — Forward-looking earnings estimates.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | OneToOne → Symbol | Symbol |
| `data` | JSONField | Estimates |

**AnalystRecommendation**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | OneToOne → Symbol | Symbol |
| `data` | JSONField | List of recommendations |

**AnalystPriceTarget**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | OneToOne → Symbol | Symbol |
| `data` | JSONField | Target prices |

**OwnershipSnapshot**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | FK → Symbol | Symbol |
| `holder_type` | CharField(30) | institutional, major, insider_transactions |
| `data` | JSONField | Ownership data |

**NewsItem**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | FK → Symbol | Symbol |
| `title` | CharField(500) | Title |
| `link` | URLField(1000) | URL |
| `published_at` | DateTimeField | Publish time |
| `publisher` | CharField(200) | Publisher |
| `summary` | TextField | Summary |

**WatchlistTablePreference** — User preferences for the watchlist UI.

| Field | Type | Description |
|-------|------|-------------|
| `key` | CharField(64), unique | Preference key |
| `value` | JSONField | Preferences (e.g. visible columns) |

---

## Data Flow Summary

### Xero → TM1 Pipeline

```
Xero API
  │
  ▼
xero_auth (OAuth2 tokens)
  │
  ▼
xero_metadata (accounts, tracking, contacts)
  │
  ▼
xero_data (journals, transactions, documents)
  │
  ▼
xero_cube (trail balance, P&L, balance sheet)
  │
  ▼
planning_analytics pipeline
  │
  ▼
TM1 Server (cubes populated via TI processes)
```

### AI Agent RAG Flow

```
SystemDocument / KnowledgeCorpus
  │
  ▼
vector_store.py (chunk + embed with OpenAI)
  │
  ▼
KnowledgeChunkEmbedding (stored in PostgreSQL)
  │
  ▼
chat_runner.py (semantic search → context injection → LLM response)
```
