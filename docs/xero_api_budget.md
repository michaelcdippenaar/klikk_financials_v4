# Xero API budget — what this app actually gets

`CLAUDE.md` carries this too, but `CLAUDE.md` is gitignored in this repo, so it
never reaches a fresh clone. This file is the tracked copy. Keep them in step.

## The daily cap is 1,000 per tenant, not 5,000

Xero's public rate-limit page says **5,000 calls per tenant per day**. This app
gets **1,000**.

Measured, not inferred:

| Date | How | Result |
|---|---|---|
| 2026-08-19 | After the ids-queue lockout | ceiling observed at 1,000/tenant |
| 2026-09-03 | Re-confirmed on Tremly and Dippenaar during the attachment backfill | 1,000/tenant |

The cause is Xero's **2 March 2026 move to tiered pricing**: Starter is
1,000/day, every other tier is 5,000. So the cap is a property of **this app's
tier**, not a constant you can read off the docs — and this app is on Starter.

## What that means when planning a job

- **Budget against 1,000.** A job sized against 5,000 commits to five times the
  available budget. Both August 2026 blowouts were this mistake, and it nearly
  happened a third time on 2026-09-03: a Tremly attachment backfill was sized at
  ~1,800 calls before the real ceiling was checked.
- **Read `X-DayLimit-Remaining` off a real response.** Never compute remaining
  calls by arithmetic — the daily window is fixed and resets at an unpublished,
  per-tenant time.
- **Leave ~300 calls of headroom.** The 02:45 all-tenants nightly needs them. A
  daytime job that drains the budget silently breaks the overnight one, and the
  failure shows up as stale data rather than as an error.
- **Chunk by tenant and date range.** Never an unbounded all-tenants sweep — see
  the hard rules in `CLAUDE.md`.
- The `xero-api-integration` skill carries the full operating rules (429
  handling, token rotation, single-flight refresh, attachment discovery).

## Related

- `docs/vm_scheduled_jobs.md` — the cron jobs that consume this budget.
- `scripts/xero-doc-backfill.sh` — attachment discovery; enumerates tenants.
