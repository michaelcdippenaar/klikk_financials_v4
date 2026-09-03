# Deploying Klikk Financials

`CLAUDE.md` describes the per-commit release model. This file is the tracked
copy of the rules that are not obvious from it, each one learned by breaking
something. `CLAUDE.md` is gitignored in this repo, so it never reaches a fresh
clone — keep the two in step.

## Every `compose up` must carry the backend override

```bash
sudo docker compose -f docker-compose.yml \
     -f backend-<sha>-worker-on.compose.override.yml \
     up -d <service...>
```

**Including when you are targeting a different service.** On 2026-09-03 a
console deploy was run as:

```bash
sudo docker compose up -d --build klikk-financials-console   # WRONG
```

Compose recreated `klikk-financials-v4` as well, from the BASE compose file
only. It came up on the base image with `/app` bind-mounted on
`/srv/klikk-financials/compose/klikk_financials_v4` — the **scratch checkout**,
twelve commits behind — and served stale code until someone noticed. Nothing
errored. The tell was an endpoint flipping 401 → 404.

Recovery is one command: re-run `compose up` **with** the correct override.

The blast radius is small only because per-commit release dirs and override
files exist. Do not treat the base `docker-compose.yml` as safe to invoke on
its own.

## The scratch checkout does not serve

`/srv/klikk-financials/compose/klikk_financials_v4` is a scratch checkout. It
is deliberately NOT kept at `origin/main` and is routinely many commits behind.
The containers bind-mount `klikk_financials_v4_releases/<full-sha>` instead.

Do not "fix" anything by fast-forwarding that clone. It buys nothing and makes
it look authoritative. Anything that resolves into it — a symlink, a bind
mount, a cron target — is running an arbitrary old commit. See
`docs/vm_scheduled_jobs.md` for how host scripts are installed instead.

## Deploy from the `origin/main` TIP, never your own commit alone

Several sessions work in this repo at once. `git archive` the tip, not your
branch: a release cut from a stale sha silently reverts everyone else's work.
On 2026-09-03 three agents raced `compose up` within one second and briefly
downgraded production.

Preserve `WEB_API_V2_INGEST_WORKER_ENABLED: "true"` when sed-ing a new override
from the previous one, and confirm afterwards that BOTH `klikk-financials-v4`
and `klikk-financials-ingest-worker` are up under their proper names — a
concurrent recreate has renamed the worker before now.

## `docker build` can hand you a cached image without your change

On 2026-09-04 a release was cut correctly and served an image that did **not**
contain the committed entrypoint change. Everything passed:

```
release dir   has the change (grep -c => 2)
build         exited 0
container     Up, pages 200, gate 401 x4
image         DOES NOT HAVE IT (grep -c => 0), layers 34 minutes old
```

The `COPY scripts/docker-entrypoint.sh` layer was reused from the previous
release even though the file differed. This is precisely the "correctly-installed
WRONG version" this runbook warns about one section down — it is not
hypothetical, it has happened.

**Check the IMAGE, not the release dir**, for anything that is baked in rather
than bind-mounted. `/app` comes from the release dir so code changes are always
live; everything else — the entrypoint, installed packages, static assets built
at image time — comes from the image:

```bash
sudo docker run --rm --entrypoint sh klikk-financials-v4:<tag> \
  -c 'grep -c "<something you just added>" /docker-entrypoint.sh'
```

If it comes back 0, rebuild with `--no-cache` and check again before deploying.
A boot-log line is the cheapest proof for entrypoint changes specifically.

## A healthy container is not evidence the schema applied

The register's DDL lives in `_ensure_table()` and runs on the FIRST AUTHENTICATED
REQUEST to the comments endpoint — not at container start. So after a release the
container is up, `/app/pipeline` is 200, the four journal endpoints are 401, and the
new column still does not exist.

Seen twice on 2026-09-03. Both times every mechanism check passed:

```
container   Up 8 seconds          <- true
pages       200 200               <- true
gate        401 401 401 401       <- true
column      assignee_key          <- STILL THE OLD NAME
log table   does not exist
```

Unauthenticated probes cannot trigger it: they are rejected before the view runs. So
verify the SCHEMA directly after any release that changes it, and trigger the DDL
deliberately rather than waiting for a user to do it:

```bash
sudo docker exec klikk-financials-v4 python manage.py shell -c \
  "from apps.xero.xero_data import pivot_comments as pc; pc._ensure_table()"
```

Then assert the specific thing that changed — the column name, the new table — not
that the command exited 0.

The failure mode this creates is the worst kind: the first real user's request runs
the migration, so the schema appears minutes later and the deploy looks fine in
between. If the DDL is wrong, the person who finds out is a user, not you.

## Verify the payload, not just the mechanism

After any release, "the command succeeded" is not evidence. Check that the
CHANGE is present in what is actually being served or run:

```bash
# the four journal endpoints must be 401 unauthenticated — a full-ledger
# disclosure lived here until 2026-08-19; see excel_addin/README.md
for u in "journals/search/?limit=1" "journals/filters/" \
         "journals/pivot/dimensions/" "journals/pivot/?rows=account_type&measure=amount"; do
  curl -sS -o /dev/null -w "%{http_code} $u\n" \
    "https://console.8-bit.space/backend/xero/data/$u"
done
curl -sS -o /dev/null -w "%{http_code}\n" https://console.8-bit.space/app/pipeline   # 200
```

Then grep the served asset, or the installed file, for the thing you changed.
A correctly-installed WRONG version passes every mechanism check — that
distinction was found while converting a host script the same day.

For the Excel add-in specifically: `lib.js`, `taskpane.html` and
`assets/*.png` must stay 200 and `README.md` / `assets/src/*` must stay 404.
A rule that closes the leak but 404s the ribbon icons is a worse outcome than
the leak.

## Related

- `CLAUDE.md` — the release-dir mechanics and per-tenant Xero rules.
- `docs/vm_scheduled_jobs.md` — cron jobs and how host scripts are installed.
- `docs/xero_api_budget.md` — the real daily cap.
