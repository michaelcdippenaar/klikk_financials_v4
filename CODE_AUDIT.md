# Klikk Financials v4 — Code Audit

> Read-only audit. No code was changed. Authored for hand-off to other agents/engineers.
> Branch: `claude/audit-app-code-ODyYm`. Date: 2026-05-22.

---

## 1. What the app is

A **Django 5.1 + DRF backend** (Python 3.10) serving as a **financial business-intelligence
platform** for Klikk/Tremly. It ingests data from accounting, banking, and market sources,
consolidates it into analytical structures, pushes it to IBM Planning Analytics (TM1) and
BigQuery, and exposes an **AI agent** (Claude/OpenAI) that queries and manipulates that data
via natural language. It is API-only — the only template is a login page; the frontend is a
separate consumer.

### Tech stack
- Django 5.1, DRF, SimpleJWT (1h access / 7d refresh, rotation + blacklist)
- PostgreSQL (+ pgvector in a separate `klikk_bi_etl` DB for RAG vectors)
- Channels (WebSockets) for live AI chat; Gunicorn/Uvicorn + WhiteNoise + nginx
- Docker / docker-compose (staging); GitHub-webhook auto-deploy
- Integrations: `xero-python`, Investec API, `yfinance` + EOD Historical Data, `TM1py`,
  `pandas-gbq`/BigQuery, `anthropic`/`openai`/`voyageai`/sentence-transformers

### Settings & deployment
- Split settings: `base / development / staging / production` (+ JSON log formatter).
  Custom lightweight `.env` loader in `base.py`.
- Docker app on port 8001, talks to host Postgres via `host-gateway`; media + Xero documents
  stored on a separate document server (192.168.1.235).
- `apps/deployment` exposes an HMAC-verified GitHub webhook that runs `deploy.sh`
  (migrate, collectstatic, restart gunicorn) on push to `main`.

---

## 2. App-by-app map

| App | Focus | External APIs | Notes |
|-----|-------|---------------|-------|
| `apps/user` | Custom `User` (AbstractUser + timestamps), JWT auth | — | register/login/refresh + nginx `auth_request` cookie check. No roles/orgs yet. |
| `apps/xero` | Accounting integration (9 sub-apps) | Xero OAuth2 + Accounting API, BigQuery | Largest area — see §3. |
| `apps/investec` | Bank + JSE securities | Investec OAuth2 client-credentials | Bank sync (dedup uuid→hash); Excel import of JSE trades/portfolio; TTM dividends. |
| `apps/financial_investments` | Market data | yfinance, EOD Historical Data | Symbols/prices/dividends/analyst/news; dividend calendar with **TM1 write-back**. |
| `apps/planning_analytics` | TM1 / Planning Analytics bridge | TM1 REST | Server config, per-user TM1 creds, TI process exec, pipeline runner, Xero→TM1 tracking map. |
| `apps/ai_agent` | MCP-style skills engine | Anthropic/OpenAI, pgvector, TM1, web search | ~19 skills, keyword routing, parallel tool exec, RAG, WebSocket chat. See §4. |
| `apps/deployment` | GitHub auto-deploy webhook | GitHub | HMAC-SHA256 verified; main branch only. |

### Xero sub-apps (layered pipeline)
`xero_auth` (OAuth2, multi-tenant tokens) → `xero_core` (`XeroTenant` + low-level API client +
token refresh) → `xero_metadata` (accounts/contacts/tracking, bulk upsert) →
`xero_data` (transactions + journals, raw→processed) → `xero_sync` (incremental sync
orchestration, scheduling, logging, Trigger/ProcessTree framework) →
`xero_cube` (OLAP: trail balance / balance sheet / P&L-by-tracking via `INSERT...SELECT`) →
`xero_integration` (BigQuery export) → `xero_validation` (compare imported vs computed;
partially implemented) → `xero_webhooks` (subscriptions + event log).

### AI agent highlights
- Pluggable Anthropic/OpenAI loop, keyword-based skill routing (only relevant tools sent),
  parallel tool execution (ThreadPoolExecutor), approval-gated risky ops, max-rounds cap.
- RAG: sentence-transformers (`all-MiniLM-L6-v2`, 384-dim), vectors in pgvector `klikk_bi_etl`.
- WebSocket chat (Channels) + REST status polling; persists sessions, messages, tool logs,
  approvals, embeddings; DB-backed `Credential` store overrides `.env`.

---

## 3. Xero sync deep-dive (focus area)

### How it works
- **Scheduled**: `xero_sync/apps.py:ready()` starts an APScheduler `BackgroundScheduler`
  (`tasks.py:start_scheduler`). A 1-minute checker (`check_and_run_scheduled_tasks`) iterates
  enabled `XeroTenantSchedule` rows and calls `run_update_task`, which is *meant* to chain
  `run_process_task` (trail balance) → `run_profit_loss_task`. A separate hourly job
  (`services_sync_check.py`) retries "out-of-sync" endpoints.
- **Manual**: `POST /xero/sync/update-models/` (`XeroUpdateModelsView`) calls the same
  `update_xero_models` service; cube/P&L have their own manual endpoints.
- **The pull** (`xero_sync/services.py:update_xero_models`): Group 1 metadata
  (`xero_metadata/services.py:update_metadata`: organisation, accounts, tracking, contacts);
  Group 2 `bank_transactions`, `invoices`, `payments`, then `manual_journals`. Each is a
  closure in `xero_core/services.py:XeroAccountingApi` that pages (page_size 100), writes each
  page via bulk managers, and stamps `XeroLastUpdate`.
- **Incremental** via `If-Modified-Since`: `XeroLastUpdate.get_utc_date_time()` returns the
  last run time (or `1901-01-01` if never).

### Findings & improvements (prioritized)

#### 1. Scheduled pipeline is broken after the "update" stage — HIGH
`tasks.py:run_update_task` runs a post-update completion check referencing two things that no
longer exist:
- `organisation` — never defined in scope (only `tenant`/`schedule` exist). `tasks.py:90,107` → `NameError`.
- `last_update.end_time` — `end_time` was **removed in migration `0010`** (2025-11-25); the
  model now has only `date`. `tasks.py:93,108` → `AttributeError`.

This block runs *after* `log_entry.mark_completed()`, throws, is swallowed by the broad
`except Exception` at `tasks.py:151`, which then calls `mark_failed()` (so every scheduled run
is recorded **failed** even when the pull succeeded) **and skips `run_process_task`**. Net:
**scheduled trail-balance and P&L processing never run**; only the raw pull happens. Manual
endpoints still work, masking the bug.
*Fix:* delete/repair the stale check (use `tenant` and the existing `date` field), and don't
overwrite a completed log as failed.

#### 2. Incremental watermark has a data-loss race — HIGH
Each endpoint stamps `update_or_create_timestamp(now)` **after** the fetch
(`xero_core/services.py:441,486,532,578,624,670,836`). The next run uses that *post-fetch*
`now` as the floor, so records modified in Xero between fetch-start and the stamp are never
picked up → silent gaps.
*Fix:* capture `started_at` **before** the API call and store that as the watermark; consider
a small safety overlap (clock skew + `If-Modified-Since` boundary semantics).

#### 3. Multi-worker scheduler duplication — HIGH (operational)
`XeroSyncConfig.ready()` starts an in-process scheduler in **every** Gunicorn/Uvicorn worker →
N concurrent schedulers → duplicate concurrent syncs per tenant, breaching Xero limits
(60/min, 5 concurrent) and racing on writes. Also note `XERO_SCHEDULER_ENABLED` defaults
`False` in `base.py` but `ready()` defaults `True` when the attribute is missing — reconcile.
*Fix:* single-worker gate / external scheduler (management command cron, Celery beat, systemd
timer) / DB advisory lock around `run_update_task`.

#### 4. Scheduled syncs aren't counted against rate limits — MEDIUM
`log_xero_api_calls()` populates `XeroApiCallLog` only from **manual** view endpoints
(`xero_metadata/views.py:73`, `xero_data/views.py:67`, `xero_cube/views.py`, etc.). The
scheduled path computes an `api_calls` stat but never logs it → Admin Console "API calls
today" undercounts and is blind to the automation. The counter also counts *endpoints*, not
*pages* (a 5-page pull counts as 1).

#### 5. No throttle / backoff for Xero 429s — MEDIUM
Paging loops call the SDK back-to-back with no inter-call delay and no `429` / `Retry-After`
handling. Under multi-tenant or backfill load this fails mid-sync.
*Fix:* shared token-bucket limiter (60/min, honor `Retry-After`) in `XeroApiClient`.

#### 6. Dead / stale endpoints and inconsistent state — MEDIUM
- No regular `journals()` method (only `manual_journals`), yet `'journals'` persists in
  `ENDPOINT_CHOICES`, in `services_sync_check.py` retry branches, and `tasks.py` `data_endpoints`.
- `update_metadata` stamps `XeroLastUpdate` for `'organisation'`
  (`xero_metadata/services.py:76`), which isn't a valid endpoint choice.

#### 7. Partial-failure lets bad data look "fresh" — MEDIUM
`manual_journals` writes the timestamp **before** DB processing
(`xero_core/services.py:836`). If the DB write then fails, the watermark advanced but data was
never stored → permanent gap until a manual `load_all`.
*Fix:* advance watermark only after data is durably persisted (ideally same transaction).

#### 8. Client construction / module-level cache — LOW/MEDIUM
- Group 1 and Group 2 each build a fresh `XeroApiClient` (separate token refresh + credentials
  query) instead of sharing one.
- `_auth_settings_cache` (`xero_core/services.py:21`) is a never-invalidated module global;
  `XeroAuthSettings` changes require a restart.

#### 9. Security: sync endpoints are `AllowAny` — MEDIUM
`XeroUpdateModelsView` / `XeroApiCallStatsView` use `permission_classes=[AllowAny]` with
`# TODO: IsAuthenticated for production` (`xero_sync/views.py:16,63`). Anyone can trigger
tenant-wide syncs (burning rate limit) or read usage stats. Same pattern across other Xero apps.

#### 10. Observability / hygiene — LOW
Heavy `print(...)` for operational logging throughout `xero_core/services.py` (incl.
`print(response)` dumping all tracking categories) instead of the configured `logger`.

**Suggested fix order:** #1 + #2 (silent data-correctness bugs) first; #3 + #5 (multi-tenant
load) next; then #4/#6/#7 (reliability/clarity); #9 before any production exposure.

---

## 4. Cross-cutting observations

- **Loose auth defaults**: global DRF permission is `AllowAny`; views must individually opt
  into `IsAuthenticated`. Several Xero endpoints carry `# TODO` to tighten this. See
  `documentation/SECURITY_RECOMMENDATIONS.md`.
- **Committed TM1 default creds** in `base.py` (`user='mc'`, `password='pass'`, internal IP) as
  env-overridable fallbacks — ship internal infra detail.
- **Naming drift**: paths/scripts still reference `klikk_financials_v3` (repo is v4).
- **Stray artifacts in VCS**: `apps/xero.zip` (~415 KB) and a root `.xlsx` look like leftovers.
- **`xero_validation`** is partially implemented (models present; services/views noted as
  pending migration from v2).

---

## 5. Key file index (for follow-up work)

- Sync orchestration: `apps/xero/xero_sync/services.py`, `apps/xero/xero_sync/tasks.py`
- Out-of-sync retry: `apps/xero/xero_sync/services_sync_check.py`
- Sync models (`XeroLastUpdate`, schedules, triggers, process trees): `apps/xero/xero_sync/models.py`
- Xero API client + per-endpoint pull closures: `apps/xero/xero_core/services.py`
- Metadata pull: `apps/xero/xero_metadata/services.py`
- API-call logging: `apps/xero/xero_sync/api_call_logging.py`
- Watermark field removal: `apps/xero/xero_sync/migrations/0010_simplify_last_update.py`
