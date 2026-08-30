# Isolated local Klikk Financials v4 Django backend for the V2 API

This environment is intentionally separate from the existing development,
staging and production settings. It contains invented authentication and entity
records only. It does not connect to Xero, Investec, TM1, AI providers,
webhooks, schedulers or any other external source.

“V2” in this local profile refers to the `/api/v2/...` contract and isolation mode. The application being run is the Klikk Financials v4 Django backend, not a separate application named “V2 backend.”

## Safety boundary

- Settings: `klikk_business_intelligence.settings.local_v2` only.
- URLconf: health, the four `/api/v2/auth/...` POST routes, and read-only
  `POST /api/v2/graphql/` only.
- Frontend origin: `http://127.0.0.1:18080` through an exact Nginx allowlist.
- Direct backend: `http://127.0.0.1:18001`, also using the narrow URLconf.
- PostgreSQL/pgvector 16: `127.0.0.1:15432`, database name
  `klikk_v2_local` only.
- Docker network: `klikk-v2-local-isolated`, marked `internal: true`.
- Fixture: one invented user, one invented entity, one active viewer membership,
  implicit `VIEW_FINANCIALS`, eight NOT_CONFIGURED catalogue definitions, and
  no explicit capabilities, source credentials, process runs or audit events.

The proxy never forwards `/api/v2/entities/...`; manual run/retry/status/history
REST routes are therefore unavailable. The local URLconf also omits them, so a
direct-backend request cannot bypass the proxy.

## Start

Prerequisites are Docker Compose, OpenSSL and images required by the Compose
file. No repository `.env` file is used.

```bash
scripts/local-v2-init.sh
scripts/local-v2-up.sh
```

The first command creates a mode-0600 runtime file outside Git under
`${XDG_STATE_HOME:-$HOME/.local/state}/klikk-v2-local/runtime.env`. It contains
only newly generated local secrets and the invented login password. Values are
not printed. The second command builds and starts the explicitly named local
services.

The frontend contract is machine-readable at
`config/local-v2-frontend-route-allowlist.json`. Frontend configuration must
proxy only those exact routes; a broad `/api/v2` proxy is forbidden.

## Stop and deterministic purge

```bash
scripts/local-v2-down.sh
scripts/local-v2-down.sh --purge-state
```

Both forms remove only Compose project `klikk-v2-local`, containers attached to
that project, network `klikk-v2-local-isolated`, and volume
`klikk-v2-local-postgres-data`. The second form additionally removes the exact
restricted runtime file after validating that its parent directory ends in
`/klikk-v2-local`. It never targets a home directory, repository root or
unresolved wildcard.

Re-running init/up after purge recreates the same invented username, entity,
membership, preferences and source catalogue. Secret values are newly generated.

## Verification contract

Run local-specific checks with the generated runtime variables loaded:

```bash
set -a
. "${XDG_STATE_HOME:-$HOME/.local/state}/klikk-v2-local/runtime.env"
set +a
DJANGO_SETTINGS_MODULE=klikk_business_intelligence.settings.local_v2 \
  python manage.py test apps.web_api_v2.tests.test_local_v2_environment
```

Required HTTP evidence:

1. login succeeds only for `local-v2-reader` with the generated password;
2. `viewerContext` returns exactly `local-v2-entity-0001` and
   `VIEW_FINANCIALS`;
3. a foreign entity returns typed `FORBIDDEN_ENTITY`;
4. GraphQL mutation documents are rejected;
5. `/api/v2/entities/...` returns 404 and persists no run;
6. an outbound connection attempt from the backend fails because the only
   attached Docker network is internal;
7. stop/start preserves the fixture, and purge/recreate produces the same
   identities and row counts.

## Production-derived snapshots

No production-derived snapshot is enabled by this candidate. The governing
policy and inventory are in `config/local-v2-financial-snapshot-policy.json`
and `docs/local-v2-financial-snapshot.md`. Export remains blocked until the user
selects one entity or all three and DevOps approves the exact export plan.
