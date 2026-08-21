# Web API v2 browser contract

Status: candidate contract. The web application may call only the routes listed here. A missing
v2 route means the feature remains unavailable or preview-only; it is never permission to fall
back to another API version.

## Endpoint inventory

Browser authentication (SimpleJWT only):

- `POST /api/v2/auth/login/`
- `POST /api/v2/auth/refresh/`
- `POST /api/v2/auth/verify/`
- `POST /api/v2/auth/logout/`

Read-oriented composition:

- `POST /api/v2/graphql/`

Entity ingest operations:

- `POST /api/v2/entities/{entityId}/ingest/process-runs/`
- `GET /api/v2/entities/{entityId}/ingest/process-runs/`
- `GET /api/v2/entities/{entityId}/ingest/process-runs/{runId}/`
- `GET /api/v2/entities/{entityId}/ingest/process-status/`

The command/status/history routes require an active entity membership and an explicit active
`RUN_INGESTION_PROCESS` grant. Membership or role alone never grants execution. GraphQL ingest
reads require active membership; the grant only changes server-derived `nextValidAction` values.

Not yet available in v2: Xero connection administration, detailed finance reads, receipts,
audit/comments, Investec, Planning Analytics, exports, and settings. The v2 frontend must keep
those transports disabled until a reviewed route is added to this inventory.

## Authentication

Login request:

```http
POST /api/v2/auth/login/
Content-Type: application/json

{"username":"reviewer","password":"..."}
```

Success returns `user` plus `tokens.access` and `tokens.refresh`. Refresh rotates the refresh token
under the SimpleJWT policy. Logout requires a valid access token and the same user's refresh token.
It blacklists that refresh token, but cannot revoke the already-issued stateless access token:

```json
{
  "refreshTokenRevoked": true,
  "accessTokenRevoked": false,
  "accessTokenValidUntilExpiry": true
}
```

Errors use a stable envelope:

```json
{
  "error": {
    "code": "TOKEN_INVALID",
    "message": "Refresh token was not accepted.",
    "retryable": false,
    "correlationId": "..."
  }
}
```

## Ingest overview query

Source definitions are persistent entity catalogue rows. They are returned even when no selected
period has a run. Period summaries are separate and never inferred from whether the catalogue is
empty.

```graphql
query IngestOverview($entityId: ID!, $periods: [YearMonth!]!) {
  ingestOverview(input: {entityId: $entityId, periods: $periods}) {
    entityId
    selectedPeriods
    attentionCount
    stageState
    completionPercent
    sourceJobDefinitions {
      id
      key
      sourceFamily
      label
      required
      connectionState
      supportedOperations
      readCapabilities
      periodRunSummaries {
        period
        state
        latestAttemptAt
        latestSuccessAt
        freshnessAt
        records { read created updated skipped failed }
        outputs
        validation { state code message }
        prerequisites { code satisfied message }
        blockedReason { code message }
        nextValidAction
      }
    }
  }
}
```

Period state explicitly distinguishes `NOT_RUN`, `NO_PERIOD_DATA`, `NOT_CONFIGURED`,
`TEMPORARILY_UNAVAILABLE`, `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, and
`BLOCKED`. Attention counts source definitions that require action, not total definitions.
Completion counts only required definition/period units that succeeded, validated, and are not
blocked; required unavailable or unvalidated sources prevent 100%.

## Process runs

Create request:

```http
POST /api/v2/entities/tenant-uuid/ingest/process-runs/
Authorization: Bearer <browser-access-token>
Content-Type: application/json

{
  "processKey": "metadata",
  "idempotencyKey": "portal-20260821-000001",
  "periods": ["2026-07"],
  "expectedState": {
    "latestRunId": "optional-uuid",
    "latestSuccessAt": "optional-iso8601"
  },
  "retryOfRunId": null
}
```

Allowlisted keys are `metadata`, `transaction-journal-sync`, `invoice-sync`,
`process-journals`, `trail-balance`, `documents`, `aged-payables`, `aged-receivables`, and
`standard-sync`. The compatibility machine key remains `trail-balance`; its display label is
**Trial Balance**.

The first candidate executes safe individual commands synchronously. A new run is persisted as
`queued`, changed to `running`, and returned in a terminal state with HTTP 201. The connection must
allow the HTTP request to remain open for that operation; clients must not assume a 202/background
queue. `standard-sync` is intentionally returned as a durable `blocked` run with
`DURABLE_WORKER_REQUIRED` until a real background worker can safely outlive proxy timeouts.

The same entity/idempotency key and request fingerprint returns the original run with HTTP 200 and
`idempotentReplay: true`. Reusing the key for different input returns `IDEMPOTENCY_CONFLICT`. Only
one `queued` or `running` ingest run may exist per entity, so orchestration cannot race an individual
step.

When no run exists, REST status returns `not_run` (not the legacy `idle` label). A synchronous run
holds a 30-minute server lease. If the serving process dies before recording a terminal result, the
next command atomically records the stale run as retryable `failed` with code
`RUN_LEASE_EXPIRED`, writes a `lease-expired` audit event, and may then create the new run.

History is cursor-bounded (`limit` 1–50). The server owns prerequisites, permitted actions, safe
output summaries, retryability, and error messages. No API accepts module names, function names,
full-rebuild flags, raw credentials, or arbitrary command payloads.

JSON request bodies are capped at 16 KiB. Command traffic is throttled at 10 requests/hour per
authenticated user; ingest reads at 120/minute. Auth scopes are login 10/minute, refresh 30/minute,
verify 60/minute, and logout 30/minute. Deployment may lower these configured ceilings.

## Frontend store fixture

The Pinia boundary should preserve server enum values and keep preview data separately attributable:

```js
{
  entityId: 'tenant-uuid',
  selectedPeriods: ['2026-07'],
  stageState: 'ATTENTION_REQUIRED',
  completionPercent: 40,
  attentionCount: 3,
  sourceJobDefinitions: [{
    id: '17',
    key: 'trail-balance',
    label: 'Trial Balance',
    sourceFamily: 'XERO',
    required: true,
    connectionState: 'CONFIGURED',
    supportedOperations: ['trail-balance'],
    readCapabilities: ['VIEW_STATUS'],
    periodRunSummaries: [{
      period: '2026-07',
      state: 'NOT_RUN',
      latestAttemptAt: null,
      latestSuccessAt: null,
      freshnessAt: null,
      records: { read: null, created: null, updated: null, skipped: null, failed: null },
      outputs: {},
      validation: { state: 'NOT_RUN', code: null, message: null },
      prerequisites: [],
      blockedReason: null,
      nextValidAction: 'RUN'
    }]
  }]
}
```

Demo/preview entities must never be sent to these REST commands.
