# V2 ingest worker

## Why it exists

`POST /api/v2/entities/{id}/ingest/process-runs/` used to execute the Xero sync
inline. A browser request therefore held a connection for the length of a
provider sync — against the gunicorn and nginx timeouts this repository carries
three fix documents for — and consumed Xero API budget with no queue and no
recovery. Two production budget blowouts in August 2026 came from unbounded
Xero calls.

The endpoint now returns `202 Accepted` with a `queued` run. This worker claims
and executes it.

## Running it

```bash
python manage.py run_ingest_worker                 # long-running
python manage.py run_ingest_worker --once          # drain the queue and exit
python manage.py run_ingest_worker --max-runs 1    # one run, then stop
```

Several workers may run at once: claiming uses `SELECT ... FOR UPDATE SKIP
LOCKED`, so a run is never executed twice. `SIGTERM` finishes the run in hand
before stopping, rather than abandoning it mid-provider-call.

## Enabling Standard sync

`WEB_API_V2_INGEST_WORKER_ENABLED` declares that a deployment actually runs a
worker. **It defaults to false**, and while false:

- the `durable-worker` prerequisite reports unsatisfied, so the API refuses the
  run before it starts;
- `standard-sync` remains blocked even if it somehow reached the executor.

Set it only on a deployment that runs the worker. Standard sync executes all
eight stages and is the largest Xero consumer in the system — turning it on
spends real API budget against the daily limit.

Individual stages do not need the flag; they are gated by their own
prerequisites and by `RUN_INGESTION_PROCESS`.

## Recovery

Every run carries a lease. `reap_expired_runs()` runs each poll and fails any
run whose lease lapsed, marking it retryable. This matters because one active
run per entity is a hard database constraint: without reaping, a worker that
dies mid-run locks that entity out of ingestion entirely.

A reaped run is retryable, not retried. Nothing here knows whether the work
partially applied, so re-running is the operator's decision.
