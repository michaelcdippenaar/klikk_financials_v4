# Xero API budget — what this app actually gets

`CLAUDE.md` carries this too, but `CLAUDE.md` is gitignored in this repo, so it
never reaches a fresh clone. This file is the tracked copy. Keep them in step.

## The daily cap is 1,000 per tenant, not 5,000

Xero's public rate-limit page says **5,000 calls per tenant per day**. This app
gets **1,000**.

Measured, not inferred:

| Date | Tenant | Observation |
|---|---|---|
| 2026-08-19 | Klikk | the ids-queue run **died at 1,036 calls** and locked the 02:45 pipeline out — the hard ceiling data point |
| 2026-08-29 | Klikk | 968 remaining after discovery; 611 after a 500-call fetch |
| 2026-08-30 | Klikk | probe 906; 887 after discovery; 475 after a 412-call fetch |
| 2026-09-03 | Dippenaar | **991 remaining after 7 discovery calls** → a start-of-run ceiling of ~998. The tightest single point. |
| 2026-09-03 | Tremly | 969 on a 1-call pre-flight probe; stood down at 301 |

**This is an inference, not a figure Xero returned.** No `X-DayLimit-Remaining`
value above **998** has ever been observed, on any tenant, on any day — and the
2026-08-19 run died at 1,036 calls. Nothing in a Xero response says "1000".
Treat 1,000 as the working ceiling and keep reading the header rather than
trusting this number.

Second caveat: the daily window is fixed and per-tenant, and the script header
records the reset at roughly **14:26 UTC**. That time was not re-verified on
2026-09-03.

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
