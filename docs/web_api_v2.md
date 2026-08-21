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
`ingestOverview.outputs` contains only allowlisted numeric record counts. Stored output JSON, raw error
messages, blocker messages and audit metadata are never projected; known error codes use reviewed safe
copy and unknown codes collapse to `PROCESS_FAILED` or `PROCESS_BLOCKED`.


Demo/preview entities must never be sent to these REST commands.

## Overview ingest sources (ING-CONNECT-001 v1.1)

`overviewIngestSources(context: FinancialContextInput!)` is the server-owned eight-card Overview
catalogue. It is distinct from `ingestOverview`, whose eight definitions are Xero pipeline jobs.

Financial years use ending-year semantics. For an entity whose fiscal year starts in July, FY2026
resolves to 1 July 2025 through 30 June 2026. The server validates the selected months against the
entity fiscal calendar and echoes the resolved dates and ordered periods.

```graphql
query OverviewSources($context: FinancialContextInput!) {
  overviewIngestSources(context: $context) {
    context {
      entityId
      financialYear
      fiscalYearStartMonth
      startsOn
      endsOn
      selectionMode
      selectedPeriods
    }
    actionableAttentionCount
    sources {
      key
      label
      provider
      mode
      availabilityCode
      state
      userSafeReason
      remediation
      latestAttemptAt
      latestSuccessAt
      freshnessAt
      records { read created updated skipped failed }
      outputs { code label count }
      validation { state code message }
      actions {
        kind
        permitted
        processKey
        requiredCapability
        disabledCode
        disabledReason
      }
      xeroPipelineAvailable
    }
  }
}
```

```json
{
  "context": {
    "entityId": "tenant-uuid",
    "financialYear": 2026,
    "periodSelection": {
      "mode": "MONTHS",
      "months": ["2025-07"]
    }
  }
}
```

The source order is Xero, Investec bank, Investment holdings, Share transactions, WhatsApp
receipts, Email documents, Manual document uploads, and Planning Analytics targets. Phase 1 has a
live Xero projection. The remaining seven sources are persistently returned by the server with
`ENTITY_BINDING_REQUIRED`, `DURABLE_STATUS_REQUIRED`, or `NOT_IMPLEMENTED`.

For every non-`AVAILABLE` source, `records` and timestamps are null, `outputs` is empty, and
validation is `UNAVAILABLE`. Zero is returned only when a successful available source actually
recorded zero. `availabilityCode` is for machine branching; user interfaces display
`userSafeReason` and optional truthful `remediation`, never an internal code as finance copy.

## Xero pipeline workbench reads

`xeroPipelineSummary(context: FinancialContextInput!)` exposes five ordered required stages and
three supporting stages. The internal REST key remains `trail-balance`; GraphQL and display copy
use `TRIAL_BALANCE` and **Trial Balance**.

```graphql
query XeroPipeline($context: FinancialContextInput!) {
  xeroPipelineSummary(context: $context) {
    context { entityId financialYear startsOn endsOn selectedPeriods }
    state completionPercent attentionCount
    firstBlocker { stage code userSafeReason remediation }
    stages {
      key processKey label category required state latestRunId
      validation { state code }
      blocker { stage code userSafeReason }
      nextValidAction
      periodRunSummaries { period state latestRunId nextValidAction }
    }
  }
}
```

Required completion reaches 100% only when all five required stages have explicit selected-period
run evidence, succeeded validation, and no blocker. Legacy or ambiguous runs without a normalized
period link return `NO_PERIOD_DATA`; timestamps never imply period coverage.

Stage/card run totals are returned only when every contributing run period is contained by the
selected context and contributing run scopes do not overlap. A subset of a multi-period run,
overlapping multi-period runs, or ambiguous legacy evidence returns null records and empty outputs.
Multi-period counts remain run-level evidence and are never duplicated as per-period measurements.

Recent stage history is bounded to 1-20 rows:

```graphql
query XeroHistory($context: FinancialContextInput!) {
  xeroPipelineRunHistory(context: $context, stage: METADATA, limit: 10) {
    stage
    runs { id stage processKey state requestedAt startedAt finishedAt periods }
  }
}
```

Run detail is restricted to the authorized entity and selected context:

```graphql
query XeroRun($context: FinancialContextInput!, $runId: ID!) {
  xeroPipelineRunDetail(context: $context, runId: $runId) {
    id stage processKey state periods
    records { read created updated skipped failed }
    outputs { code label count }
    validation { state code message }
    retryable
    safeError { code userSafeReason correlationId remediation }
  }
}
```

All four GraphQL reads require SimpleJWT, active entity membership, and `VIEW_FINANCIALS`.
History/detail do not reuse or weaken the existing REST GET authorization. GraphQL never returns
raw process output, idempotency fingerprints, credentials, audit metadata, or raw stored error
messages. Missing and foreign runs are indistinguishable through the safe typed `RUN_NOT_FOUND`
error.

RUN and RETRY remain separate REST commands and still require the explicit active
`RUN_INGESTION_PROCESS` grant; no grant is created by migration 0003. The GraphQL `processKey`,
`latestRunId`, and action fields are read/navigation descriptors only. This candidate does not claim
ING-RUN-001 manual-start compatibility: the existing command surface does not yet enforce the
GraphQL `nextValidAction` or period/FY execution scope, and synchronous timeout behavior remains
unbounded. Frontend command enablement remains release-gated pending the separate command contract.
`standard-sync` remains blocked until a durable worker exists.

`IngestProcessRunPeriod` is the additive period-evidence model. Migration 0003 backfills only
canonical explicit values already stored in `IngestProcessRun.periods`; it never infers periods
from timestamps, fiscal years, outputs, or an empty list.
