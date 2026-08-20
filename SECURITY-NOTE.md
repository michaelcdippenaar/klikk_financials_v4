# SECURITY NOTE — the backend API is unauthenticated and internet-reachable

*Written 19 August 2026 on branch `feature/pricelist`, alongside the change that put a shared service token in front of the three pricelist **write** endpoints.*
*Status: **escalation, not a fix**. Nothing in this note has been implemented. It needs a decision from MC.*

> **STATUS UPDATE — 2026-08-20: CLOSED at the application layer.** The lockdown
> described in §4 Step 2 is implemented:
> `DEFAULT_PERMISSION_CLASSES = IsAuthenticated` project-wide; every explicit
> `AllowAny` removed except the credential bootstrap paths (`/api/auth/login/`,
> `/api/auth/refresh/`, `/api/auth/token/*`, `/api/auth/nginx-check/`,
> `/api-token-auth/`) and `/xero/callback/`; `POST /api/auth/register/` is
> `IsAdminUser`; the deploy webhook route is deleted (`apps/deployment/urls.py`);
> anonymous throttling is on (60/min).
>
> **Two surfaces are public by design, and both authenticate the SIGNATURE
> rather than the caller** — neither is a DRF view with a permission class, so
> the project-wide `IsAuthenticated` default never applies to them and removing
> their gate would be silent:
> * `/audit/slip/<sha256>/?s=` — the slip viewer (`apps/audit/slip_view.py`).
> * `/xero/data/documents/<id>/file/?s=` — the Xero source-document viewer
>   (`apps/xero/xero_data/document_views.py`). Verifies the HMAC with
>   `hmac.compare_digest` **before** any DB lookup, so an unsigned caller
>   cannot learn which document ids exist. Its sibling
>   `/xero/data/documents/search/` IS a DRF view and IS `IsAuthenticated`.
>
> Machine callers: the MCP sends `Bearer KLIKK_API_TOKEN`
> (ServiceTokenAuthentication), the Excel add-in its per-user DRF authtoken;
> all crons use `manage.py`, none call HTTP — **verified 2026-08-20** by
> reading all four scheduled jobs (`daily-klikk-financials-update.sh`,
> `daily-tm1-full-refresh.sh`, `xero-doc-backfill.sh`, `klikk-sync.sh`): every
> one goes through `docker compose exec ... python manage.py`, so DRF
> permissions are never in the path. Regression suite:
> `apps/user/test_auth_lockdown.py` + `excel_addin/README.md` curl loop.
> Still open from this note: the edge allow-list (§4 Step 1, proposal with MC),
> secret rotation for the DB password in git history, OAuth `state` validation
> on the callback, and the POPIA §19/§22 historic-exposure question (with CCO).
>
> **Also open, raised 2026-08-20 while fixing the credential-resolution 500s:**
> an authenticated user with no `XeroClientCredentials` row of their own acts
> through the first active row (`apps/xero/xero_auth/credentials.py`). Not a
> widening — before this lockdown *any anonymous caller* got that same fallback
> — but it must become a strict per-user lookup the day a login exists that is
> not MC's. **MC's call.**
>
> **New surface, 2026-08-20 (cube comment tags + @mentions):**
> `/xero/data/journals/pivot/people/` (GET/POST) is `IsAuthenticated` and was
> confirmed 401 anonymously, as were the comment endpoints — the public surface
> is unchanged. Two things about it are worth a compliance eye rather than an
> engineering one:
> * `app.cube_people` holds the **names and email addresses of third parties**
>   (MC's bookkeeper and auditors) who are not users of this system and have no
>   login. That is personal information in a new table, so it belongs in the
>   ROPA, and it needs a retention answer. Deliberately curated through the
>   endpoint — never inferred from Xero contacts, WhatsApp, or anywhere else.
> * A comment POST can cause an **outbound email to a named human**. It is
>   strictly transactional (one mention, one recipient, one email, only from a
>   POST containing that mention, never resent for the same comment+person) and
>   carries no tracking or images. It is not marketing, so POPIA §69 does not
>   bite, but it is the first thing in this codebase that emails a
>   non-user — worth knowing before the mail backend is ever configured.
>
> **And: the MCP client MC actually runs is NOT configured with a token.** The
> `klikk-financials` stdio server in the desktop client's config sets only
> `KLIKK_API_BASE_URL`, so it calls the backend anonymously and now gets 401s.
> The containerised HTTP MCP (`klikk-financials-mcp`) does have
> `KLIKK_API_TOKEN` via `klikk_portal/.env` and is unaffected. Placing the
> token in the client config is MC's decision — it is the shared,
> write-capable service token.

---

## 1. Why this file exists

The pricelist change on this branch added `HasServiceToken` to `POST /api/pricelist/items/`, `PATCH|PUT /api/pricelist/items/<code>/` and `POST /api/pricelist/items/<code>/prices/`. That closes three writes in one app.

It does not close anything else, and while scoping it we enumerated what "anything else" actually is. The answer is larger than a pricelist decision, so it is written down here rather than fixed unilaterally.

**The finding in one sentence: the Django API is published to the internet at `https://console.8-bit.space/backend/` with `DEFAULT_PERMISSION_CLASSES = ['rest_framework.permissions.AllowAny']`, so roughly ninety endpoints — including the complete general ledger, the Investec bank data, and a shell-executing deploy webhook — answer to any anonymous caller.**

### How this was verified

- Runtime introspection of the live URL resolver inside the running `klikk-financials-v4` container (not a grep of the source).
- 15 **GET-only** probes against `https://console.8-bit.space/backend/...`. No POST, PUT, PATCH or DELETE was sent anywhere. No sync / import / refresh / vectorise / deployment path was probed, even by GET.
- The live surface is the **`main`** branch (`8d592eb`), bind-mounted from `/srv/klikk-financials/compose/klikk_financials_v4`. This branch is **not deployed**, so `/api/pricelist/` is not yet part of the public surface at all.

---

## 2. Public **read** endpoints — the part the brief asked for

All confirmed **200 unauthenticated from the public internet**. Line numbers cite the live `main` tree.

| Prefix | Example path | Auth posture (file:line) | What an anonymous caller gets |
|---|---|---|---|
| `/xero/data/` | `/xero/data/journals/search/?limit=1000` | `AllowAny` — `apps/xero/xero_data/views.py:42` | **271,740 GL journal lines** — account code/name/type, amount, debit, credit, tax, contact name, description, reference, tracking. The whole book. |
| `/xero/data/` | `/invoices/`, `/quotes/`, `/aged-payables/`, `/aged-receivables/` | `AllowAny` — `views.py:828, 640, 463, 502` | Invoice and quote values, contact names, amounts due, ageing buckets |
| `/xero/metadata/` | `/contacts/`, `/accounts/`, `/tracking/` | `AllowAny` — `apps/xero/xero_metadata/views.py:102, 183, 147` | Full chart of accounts; full supplier/customer contact list |
| `/xero/auth/` | `/status/`, `/initiate/` | `AllowAny` — `apps/xero/xero_auth/views.py:244, 32` | Tenant names/ids, token expiry, refresh-token presence; `/initiate/` returns the real Xero **client_id** in cleartext |
| `/xero/core/` | `/tenants/` | `AllowAny` — `apps/xero/xero_core/views.py:14` | Tenant ids/names, token expiry |
| `/xero/sync/` | `/api-call-stats/`, `/process-status/` | `AllowAny` — `apps/xero/xero_sync/views.py:63, 97` | Xero quota telemetry — tells a caller how much API budget is left to burn |
| `/api/investec/` | `/bank/accounts/` | PUBLIC (no decl.) — `apps/investec/views.py:1950` | **14 real Investec accounts, with `account_number`** |
| `/api/investec/` | `/bank/transactions/`, `/export/`, `/bank/reports/costs/` | PUBLIC — `views.py:1968, 2279, 2048` | Dated bank transactions: amount, description, **running balance**, per account |
| `/api/investec/` | `/bank/beneficiaries/` | PUBLIC — `views.py:2365` | Beneficiary name, **account number, branch code, cell number, email, last payment**. Currently 0 rows only because the Investec beneficiary scope is still 403-blocked. |
| `/api/personal-expenses/` | `/report/`, `/transactions/` | PUBLIC — `apps/personal_expenses/views.py:77, 147` | **MC's personal-expense classification** — every bank transaction and its category, by month and account |
| `/api/financial-investments/` | 17 GET symbol / dividend paths | PUBLIC — `apps/financial_investments/views.py:18-333` | Holdings, buy transactions, dividend forecasts |
| `/audit/` | `/audit/checks/` | PUBLIC — `apps/audit/views.py:46` | The full year-end check registry **including each check's `sql_text`** — an effective map of the finance schema |
| `/api/ai-agent/` | `/agent-status/` | `AllowAny` — `apps/ai_agent/views.py:1666` | Live agent status and in-flight tool-call names |
| `/api/planning-analytics/` | `/tm1/config/` | `AllowAny` — `apps/planning_analytics/views.py:122` | TM1 base URL and username (password masked) |
| `/api/pricelist/` *(branch only)* | `/quote/`, `/export/`, `/items/<c>/price/` | PUBLIC — `apps/pricelist/views.py:332, 363, 277` | Rate card and customer-specific pricing. `/quote/` is write-shaped but a **pure calculation that persists nothing**. |

**POPIA note.** The Investec, personal-expenses and Xero contact endpoints publish personal information (bank account numbers, beneficiary cell numbers and email addresses, named counterparties, MC's personal spending). This is a §19 security-safeguards exposure and, if accessed, a §22 notifiable breach. That judgement belongs to the CCO, not to engineering — flagging it, not deciding it.

---

## 3. Worse than reads — please do not stop at section 2

The brief asked about reads. Reads are not the worst of it, and it would be negligent to file this note without saying so.

### Critical

- **`POST /deployment/webhook/github/` runs a shell script for any anonymous caller, with the signature check disabled.** `GITHUB_WEBHOOK_SECRET` is present-but-empty in the container, and `verify_github_signature` **returns `True` when the secret is falsy** — `apps/deployment/views.py:36-38`, reaching `subprocess.run(['bash', deploy_script])` at `:140-146`.
  *Calibration:* this is **not** being called RCE today. `scripts/deploy.sh` hardcodes `PROJECT_DIR=/home/mc/apps/klikk_financials_v3`, which does not exist in the container, and `git`/`sudo` are not installed there — it dies at `cd … || exit 1`. Current impact is unauthenticated subprocess spawn plus internal-path disclosure. Confidence the signature bypass is real: ~99%. Confidence it is presently harmless: ~90% — deliberately not tested. It is rated Critical because it is one path-fix away from unauthenticated deploy, and only a bug is preventing that.
- **`POST /xero/auth/credentials/` lets an anonymous caller overwrite Klikk's Xero OAuth `client_id` / `client_secret`** — `apps/xero/xero_auth/views.py:312, 360-372`. One request breaks every Xero sync, or substitutes an attacker-controlled OAuth app.
- **`POST /audit/checks/` accepts caller-supplied SQL against the finance database** — `apps/audit/views.py:46, 76-80`. The guard in `apps/audit/services.py:58-80` is genuinely decent (single statement, must start SELECT/WITH, keyword denylist, EXPLAIN under `transaction_read_only`) and we could not break it by reading it. Confidence it is bypassable: **low, ~25% — a hunch, not a finding.** The finding is that it is anonymously reachable at all.

### High

- **An unauthenticated *GET* that burns Xero API quota**: `GET /xero/validation/import-profit-loss/` — `apps/xero/xero_validation/views/profit_loss_views.py:30-34`, where `get()` is literally `return self.post(request)`. Given the 2026-08-18 quota blowout and Xero's rolling 24-hour limit, a crawler can exhaust the budget with no attacker intent whatsoever. This needs no POST, which is why it ranks above the other amplifiers.
- **Unauthenticated third-party-API amplifiers**, each with a direct financial cost: `POST /xero/sync/update/` (`apps/xero/xero_sync/views.py:16`), `POST /xero/metadata/update/` (`views.py:45`), six `POST /xero/data/*/sync/` triggers, `POST /api/investec/bank/sync/` (`apps/investec/views.py:2331`), `POST /api/financial-investments/symbols/<s>/refresh/` (`views.py:172`), and `POST .../vectorize-articles/` (`views.py:341`) which **bills MC's OpenAI key from an anonymous request**.
- **Public self-registration issues working JWTs** — `apps/user/views.py:39, 96-127`: `is_active=True`, access and refresh tokens returned in the response body, no email verification, no throttle. **This is load-bearing for the recommendation in §4.**
- **`POST /api/planning-analytics/tm1/config/` rewrites the TM1 base URL and credentials unauthenticated** — `apps/planning_analytics/views.py:122, 136-160`. Point `base_url` at an attacker host and the next TM1 call ships credentials there.
- **Unauthenticated mutation of MC's personal-expense classification** — `apps/personal_expenses/views.py:204, 238, 272, 303`. Year-end work depends on the integrity of that data.

### Medium

- Unauthenticated file upload and bulk insert into the investment ledger — `apps/investec/views.py:379, 1478, 1593`.
- **No rate limiting anywhere.** `REST_FRAMEWORK` has no `DEFAULT_THROTTLE_CLASSES` (verified at runtime). `/api/auth/login/` and `/api-token-auth/` are unthrottled credential-stuffing targets.
- Secrets in the repo: `klikk_business_intelligence/settings/staging.py:64` and `development.py:39, 52` carry a real-looking DB password as a default. Not reachable from the internet (Postgres binds `192.168.1.133:5432`) but it is in git history and warrants a rotation decision.

### Explicitly clean — verified, not assumed

- **No code path anywhere writes to Xero.** A sweep of `apps/xero/` for outbound mutating HTTP returns exactly two hits: a test mock (`xero_auth/tests.py:105`) and the OAuth token refresh (`xero_core/services.py:529`). MC's hard rule is not violated by any public endpoint. This was the thing most expected to be found, and it was not there.
- `/xero/cube/*` — all seven endpoints `IsAuthenticated`, confirmed by a live 401. The comment at `apps/xero/xero_cube/views.py:18-23` is accurate and is the best security documentation in the repo.
- `/api/ai-agent/*` — 30 of 33 endpoints `IsAuthenticated`; **anonymous LLM invocation is not possible**. `AI_AGENT_DISABLE_SECURITY` verified unset at runtime.
- `/api/planning-analytics/*` — 19 of 21 `IsAuthenticated`.
- `DEBUG=False`, `SECRET_KEY` not the default, `CORS_ALLOW_ALL_ORIGINS` unset, `ALLOWED_HOSTS` explicit.

### Could not verify

- **The Caddy edge config.** `console.8-bit.space` resolves to `102.135.240.221`; the VM is `192.168.1.133`. Neither `/etc/caddy` nor `/etc/nginx` exists on VM 133. We cannot say whether the edge adds auth — but the 200s prove it adds none to `/backend/*`. **Someone needs to identify that host.**
- Whether `:8001` and `:8787` are directly exposed. Both bind `0.0.0.0` on the VM. Probes from MC's Mac failed to connect, but that Mac may be on the home LAN where NAT hairpin is known to break. Confidence they are firewalled: **~60%**. Needs one probe from an off-network host.

---

## 4. Recommended minimal next step — decision required, NOT implemented

The two options in the brief were (a) an nginx/Caddy allow-list or basic auth in front of `console.8-bit.space/backend/`, and (b) flipping DRF's `DEFAULT_PERMISSION_CLASSES` to `IsAuthenticated` plus a service token for the MCP. **Recommendation: neither alone. Do a small step 0 first, then (a), then (b) properly.**

**Why (b) alone does not work.** `POST /api/auth/register/` is `AllowAny`, creates active users and returns a JWT (`apps/user/views.py:39, 96-127`). An attacker registers and reads all 271,740 journal lines again. (b) is only meaningful if registration is closed in the same change. (b) also cannot touch `/admin/` (Django, not DRF) or `/deployment/webhook/github/` (an explicit `@permission_classes([AllowAny])` that a default change cannot override), so the Critical finding survives (b) entirely.

**Why (a) alone does not work.** It closes everything at once, including the deploy webhook, and it is the only option that helps tonight. But the app keeps `AllowAny` on ~90 endpoints, so the exposure returns the moment anything else can route to `:8001` — a second vhost, a container on the same Docker network, a tunnel, or the LAN. It is a tourniquet, not a fix.

### Proposed order

**Step 0 — minutes, do first, independent of the (a)/(b) decision.** Either set a real `GITHUB_WEBHOOK_SECRET`, or delete `path('deployment/', ...)` from `klikk_business_intelligence/urls.py:57`. GitHub-webhook deploys are not in use on this VM — the script targets a bare-metal path that does not exist. **Deleting the route is the smaller, safer change and removes the Critical finding completely.** Highest value per minute in this note.

**Step 1 — hours. (a) at the edge.** Basic auth on `/backend/*`, with the credential added to the console's fetch layer; or an IP allow-list if the console is only used from known networks.
- *Breaks:* the console until the credential is added. The MCP is unaffected today — it talks to `http://192.168.1.133:8001` directly, not through the edge.
- *Carve-out that must not be missed:* `XERO_REDIRECT_URI=https://console.8-bit.space/xero/callback/` must be excluded from the auth, or Xero reconnection breaks.
- *Effort:* M, mostly logistics — the Caddy host still has to be located.

**Step 2 — a day or two. (b), done properly.** Most of the plumbing already exists on this branch:
1. Land `klikk_business_intelligence/permissions.py` (`ServiceTokenAuthentication`, prepended to `DEFAULT_AUTHENTICATION_CLASSES`). Note it **must** be an authentication class, not merely a permission class — `JWTAuthentication` raises `InvalidToken` (401) on an opaque bearer before any permission runs.
2. Set `KLIKK_API_TOKEN` on **both** the Django container and the MCP container. `server.mjs` already reads it and already sends `Authorization: Bearer`. The MCP env has `KLIKK_MCP_AUTH_TOKEN` but **not** `KLIKK_API_TOKEN` — that missing variable is exactly the blocker the `xero_cube` comment was waiting on.
3. Flip `DEFAULT_PERMISSION_CLASSES` to `IsAuthenticated`.
4. **Close `/api/auth/register/`** (remove it or gate on `IsAdminUser`). Non-negotiable; without it, step 3 is theatre.
5. Re-add explicit `AllowAny` to exactly four paths: `/api/auth/login/`, `/api/auth/token/*`, `/api/auth/refresh/`, `/xero/callback/`.
- *Breaks:* console pages that currently rely on anonymous calls — expect several, since the console was built against an open API. Budget a click-through of every page. Also any cron or script hitting `:8001` unauthenticated; enumerate those before flipping.
- *Effort:* M–L, dominated by console regression testing.

**Step 3 — small, high value.** Add `DEFAULT_THROTTLE_CLASSES` / `DEFAULT_THROTTLE_RATES` with an anonymous rate. Worth having even after auth, and it caps the damage of any future `AllowAny` regression.

### An option to reject

Adding `IsAuthenticated` view-by-view across the ~40 `AllowAny` declarations. It is tempting because it is incremental, but it is precisely this codebase's existing failure mode: 30+ `# TODO: Change to IsAuthenticated for production` comments going back years, one app fixed in June 2026, the rest still open. **The default is what is wrong; fix the default.**

---

## 5. What this branch does and does not change

**Does:** requires `Authorization: Bearer <KLIKK_API_TOKEN>` (or a logged-in console user) on the three pricelist write actions. Adds a reusable `ServiceTokenAuthentication` / `HasServiceToken` pair that Step 2 above can apply project-wide.

**Does not:** change any other endpoint's auth posture; close any read; touch the deploy webhook, registration, throttling, or the edge. `ServiceTokenAuthentication` returns `None` unless the token matches exactly, and implements `authenticate_header()` returning simplejwt's exact `Bearer realm="api"` string, so the project's existing 401-vs-403 behaviour is preserved byte for byte.

**Owner: MC.** Steps 0-3 are escalated, not actioned.
