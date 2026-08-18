"""Sync Xero SYSTEM journals for one tenant.

Xero posts some journals itself with no corresponding transaction or manual
journal: payroll runs (PAYSLIP/PAYRUN), fixed-asset depreciation, conversion
balances, expense claims, interest accruals, etc. These only surface via the
Journals API (GET /Journals, offset-paged by JournalNumber, 100 per page).
The transaction pipeline mirrors document-sourced journals (invoices,
payments, bank transactions, transfers) and the manual-journal pipeline
mirrors MANJOURNAL, so this command ingests ONLY journals whose SourceType
is NOT covered by those pipelines, as legs in xero_data_xerojournals with
journal_type='system_journal'.

The trial-balance build (XeroTrailBalanceManager.consolidate_journals)
filters journal_type != 'journal', so system_journal legs flow into the
trail balance automatically.

Fetching uses `requests` directly (bearer token via the app's XeroApiClient
token machinery) with an explicit per-call timeout — the raw xero-python
AccountingApi call can block forever on a dead socket. Pages are ingested
as they arrive and the completed offset is checkpointed to
/tmp/sync_system_journals_<tenant>.ckpt so `--resume` continues instead of
restarting. Progress is printed AND appended (flushed) to
/tmp/sync_system_journals.log.

Idempotency: a fresh (non-resume) run deletes all existing system_journal
legs for the tenant before ingesting (full-replace semantics); page inserts
use ignore_conflicts so overlapping reruns are safe.

SCOPE WARNING: only run this for tenants whose bookkeeping did NOT
re-capture system postings as manual journals. Klikk re-captured
payroll/depreciation as manual journals, so ingesting Klikk system journals
would double-count. --tenant is therefore mandatory; there is deliberately
no --all-tenants.
"""
import datetime
import json
import os
import re
import time
from collections import Counter
from decimal import Decimal

import requests
from django.core.management.base import BaseCommand, CommandError

from apps.xero.xero_core.models import XeroTenant

# SourceTypes already mirrored by the transaction pipeline (documents,
# payments, bank transactions, transfers) or the manual-journal pipeline.
# Ingesting these again would double-count.
COVERED_SOURCE_TYPES = {
    'ACCREC', 'ACCPAY', 'ACCRECCREDIT', 'ACCPAYCREDIT',
    'ACCRECPAYMENT', 'ACCPAYPAYMENT',
    'ARPREPAYMENT', 'APPREPAYMENT', 'AROVERPAYMENT', 'APOVERPAYMENT',
    'CASHREC', 'CASHPAID', 'TRANSFER', 'MANJOURNAL',
}

PAGE_SIZE = 100  # Xero Journals API returns at most 100 journals per call
JOURNALS_URL = 'https://api.xero.com/api.xro/2.0/Journals'
LOG_FILE = '/tmp/sync_system_journals.log'
REQUEST_TIMEOUT = 60  # seconds; never let a call hang
MAX_TRIES = 5


def _parse_journal_date(raw):
    """Parse JournalDate: .NET /Date(ms)/ strings, ISO strings, numeric epoch."""
    if isinstance(raw, datetime.datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=datetime.timezone.utc)
        return raw
    if isinstance(raw, datetime.date):
        return datetime.datetime(raw.year, raw.month, raw.day,
                                 tzinfo=datetime.timezone.utc)
    if isinstance(raw, str):
        match = re.match(r'/Date\((\d+)([+-]\d+)?\)/', raw)
        if match:
            try:
                return datetime.datetime.fromtimestamp(
                    int(match.group(1)) / 1000.0, tz=datetime.timezone.utc)
            except (ValueError, TypeError):
                return None
        try:
            return datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    if isinstance(raw, (int, float)):
        try:
            ts = float(raw)
            if ts > 946684800000:  # > Jan 1 2000 in ms -> milliseconds
                ts /= 1000.0
            return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        except (ValueError, TypeError):
            return None
    return None


class Command(BaseCommand):
    help = ("Fetch ALL journals for one tenant from Xero's Journals API and "
            "ingest system journals (SourceType not covered by the "
            "transaction/manual-journal pipelines) as "
            "journal_type='system_journal' legs. Streaming + checkpointed; "
            "full-replace per tenant on a fresh run; --resume continues an "
            "interrupted run.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant', required=True,
            help='Xero tenant_id (mandatory — never run blanket across '
                 'tenants; see module docstring for the double-count risk).')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Fetch and classify only; do not write any legs.')
        parser.add_argument(
            '--resume', action='store_true',
            help='Resume from the checkpoint file instead of wiping and '
                 'restarting at offset 0.')

    # ------------------------------------------------------------------ util

    def _log(self, msg, style=None):
        line = f'{datetime.datetime.now():%H:%M:%S} {msg}'
        self.stdout.write(style(msg) if style else msg)
        self.stdout.flush()
        try:
            with open(LOG_FILE, 'a') as fh:
                fh.write(line + '\n')
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass

    # ---------------------------------------------------------------- handle

    def handle(self, *args, **opts):
        tenant_id = opts['tenant']
        dry_run = opts.get('dry_run', False)
        resume = opts.get('resume', False)
        try:
            organisation = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            raise CommandError(f'Tenant {tenant_id} not found')

        ckpt_path = f'/tmp/sync_system_journals_{tenant_id}.ckpt'
        state = {
            'offset': 0,
            'source_type_counts': {},
            'journals_seen': 0,
            'journals_ingested': 0,
            'journals_skipped': 0,
            'legs_created': 0,
        }
        if resume and os.path.exists(ckpt_path):
            with open(ckpt_path) as fh:
                state.update(json.load(fh))
            self._log(f'RESUMING from checkpoint: offset={state["offset"]}, '
                      f'{state["legs_created"]} legs already created')
        elif resume:
            self._log(f'--resume given but no checkpoint at {ckpt_path}; '
                      f'starting fresh')
            resume = False

        from apps.xero.xero_data.models import XeroJournals

        if not dry_run and not resume:
            deleted = XeroJournals.objects.filter(
                organisation=organisation,
                journal_type='system_journal',
            ).delete()[0]
            self._log(f'Fresh run: deleted {deleted} existing '
                      f'system_journal legs for {organisation.tenant_name}')

        token_provider = _TokenProvider(tenant_id)
        lookups = self._build_lookups(organisation)
        counts = Counter(
            {k: v for k, v in state['source_type_counts'].items()})

        session = requests.Session()
        offset = state['offset']
        page_num = 0
        while True:
            page = self._fetch_page(session, token_provider, tenant_id, offset)
            page_num += 1
            self._log(f'page {page_num} offset={offset} got={len(page)}')
            if not page:
                break

            page_stats = self._process_page(
                organisation, page, counts, lookups, dry_run)
            state['journals_seen'] += len(page)
            state['journals_ingested'] += page_stats['ingested']
            state['journals_skipped'] += page_stats['skipped']
            state['legs_created'] += page_stats['legs']

            max_number = offset
            for j in page:
                number = j.get('JournalNumber') or 0
                if number > max_number:
                    max_number = number
            offset = max_number
            state['offset'] = offset
            state['source_type_counts'] = dict(counts)
            if not dry_run:
                with open(ckpt_path, 'w') as fh:
                    json.dump(state, fh)

            if len(page) < PAGE_SIZE:
                break
            time.sleep(1.1)  # stay inside Xero's 60 calls/minute

        self._log(f'Fetch complete: {state["journals_seen"]} journals seen '
                  f'this run (final offset {offset})')
        self._log('Distinct SourceTypes found (cumulative):')
        for st, count in sorted(counts.items()):
            decision = ('SKIP (covered by transaction/manual pipeline)'
                        if st in COVERED_SOURCE_TYPES else 'INGEST (system)')
            self._log(f'  {st:<16} {count:>7}  -> {decision}')
        self._log(
            f"DONE: ingested {state['journals_ingested']} system journals "
            f"as {state['legs_created']} system_journal legs "
            f"({state['journals_skipped']} journals skipped: unresolvable "
            f"account or bad date)",
            style=self.style.SUCCESS)
        if dry_run:
            self._log('Dry run: no legs written', style=self.style.SUCCESS)

    # ----------------------------------------------------------------- fetch

    def _fetch_page(self, session, token_provider, tenant_id, offset):
        """One GET Journals call with explicit timeout and capped backoff."""
        backoff = 5
        for attempt in range(1, MAX_TRIES + 1):
            try:
                resp = session.get(
                    JOURNALS_URL,
                    params={'offset': offset},
                    headers={
                        'Authorization': f'Bearer {token_provider.access_token()}',
                        'Xero-tenant-id': tenant_id,
                        'Accept': 'application/json',
                    },
                    timeout=REQUEST_TIMEOUT,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._log(f'  attempt {attempt}/{MAX_TRIES} at offset={offset}: '
                          f'{type(exc).__name__}: {exc}; retrying in {backoff}s',
                          style=self.style.WARNING)
                if attempt == MAX_TRIES:
                    raise CommandError(
                        f'Giving up at offset={offset} after {MAX_TRIES} '
                        f'timeouts/connection errors: {exc}')
                time.sleep(backoff)
                backoff = min(backoff * 2, 80)
                continue

            if resp.status_code == 200:
                return resp.json().get('Journals', []) or []
            if resp.status_code == 401:
                self._log(f'  401 at offset={offset}: forcing token refresh',
                          style=self.style.WARNING)
                token_provider.force_refresh()
                continue
            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', '60') or 60)
                if retry_after > 300:
                    raise CommandError(
                        f'Xero rate limit at offset={offset}: '
                        f'Retry-After={retry_after}s (likely the daily tenant '
                        f'limit). Aborting; rerun later with --resume.')
                self._log(f'  429 at offset={offset}: Retry-After='
                          f'{retry_after}s, waiting (attempt {attempt}/{MAX_TRIES})',
                          style=self.style.WARNING)
                if attempt == MAX_TRIES:
                    raise CommandError(
                        f'Still rate-limited at offset={offset} after '
                        f'{MAX_TRIES} attempts; rerun later with --resume.')
                time.sleep(retry_after + 1)
                continue
            raise CommandError(
                f'Xero returned HTTP {resp.status_code} at offset={offset}: '
                f'{resp.text[:500]}')
        raise CommandError(f'Exhausted retries at offset={offset}')

    # ---------------------------------------------------------------- ingest

    def _build_lookups(self, organisation):
        from apps.xero.xero_metadata.models import XeroAccount, XeroTracking
        return {
            'by_id': {
                a.account_id: a
                for a in XeroAccount.objects.filter(organisation=organisation)
            },
            'by_code': {
                a.code: a
                for a in XeroAccount.objects.filter(organisation=organisation)
                if a.code
            },
            'trackings': {
                t.option_id: t
                for t in XeroTracking.objects.filter(organisation=organisation)
            },
        }

    def _process_page(self, organisation, page, counts, lookups, dry_run):
        """Classify one page and ingest its system journals. Returns stats."""
        from apps.xero.xero_data.models import XeroJournals, _resolve_tracking_slot

        legs = []
        ingested = 0
        skipped = 0
        for j in page:
            source_type = j.get('SourceType') or 'NONE'
            counts[source_type] += 1
            if source_type in COVERED_SOURCE_TYPES:
                continue

            journal_id = j.get('JournalID')
            journal_number = j.get('JournalNumber') or 0
            reference = j.get('Reference') or ''
            date = _parse_journal_date(j.get('JournalDate'))
            if not journal_id or date is None:
                self._log(f'  skipping journal {journal_id or "<no id>"} '
                          f'(#{journal_number}): unparseable JournalDate '
                          f'{j.get("JournalDate")!r}',
                          style=self.style.WARNING)
                skipped += 1
                continue

            journal_legs = []
            unresolved = None
            for line_index, jl in enumerate(j.get('JournalLines') or []):
                account = lookups['by_id'].get(jl.get('AccountID'))
                if account is None:
                    account = lookups['by_code'].get(jl.get('AccountCode'))
                if account is None:
                    unresolved = (jl.get('AccountID'), jl.get('AccountCode'))
                    break
                amount = Decimal(str(jl.get('NetAmount') or 0))
                tax_amount = Decimal(str(jl.get('TaxAmount') or 0))
                leg = XeroJournals(
                    organisation=organisation,
                    # Same leg-id convention as manual journals:
                    # {JournalID}_{lineindex}
                    journal_id=f'{journal_id}_{line_index}',
                    journal_number=journal_number,
                    journal_type='system_journal',
                    account=account,
                    date=date,
                    description=jl.get('Description') or '',
                    reference=reference,
                    amount=amount,
                    debit=max(amount, Decimal('0')),
                    credit=min(amount, Decimal('0')),
                    tax_amount=tax_amount,
                )
                for idx, t in enumerate(jl.get('TrackingCategories') or []):
                    tracking_obj = lookups['trackings'].get(
                        t.get('TrackingOptionID'))
                    if tracking_obj:
                        slot = _resolve_tracking_slot(
                            tracking_obj, organisation, idx)
                        if slot == 1:
                            leg.tracking1_id = tracking_obj.id
                        elif slot == 2:
                            leg.tracking2_id = tracking_obj.id
                journal_legs.append(leg)

            if unresolved is not None:
                # Never ingest a partial journal: dropping one leg would
                # unbalance the trial balance by that leg's amount.
                self._log(f'  skipping WHOLE journal {journal_id} '
                          f'(#{journal_number}, {source_type}): unresolvable '
                          f'account id={unresolved[0]!r} code={unresolved[1]!r}',
                          style=self.style.WARNING)
                skipped += 1
                continue

            legs.extend(journal_legs)
            ingested += 1

        created = 0
        if legs and not dry_run:
            # ignore_conflicts: page overlap on resume must not duplicate legs
            created = len(XeroJournals.objects.bulk_create(
                legs, ignore_conflicts=True))
        elif dry_run:
            created = len(legs)

        return {'ingested': ingested, 'skipped': skipped, 'legs': created}


class _TokenProvider:
    """Bearer-token source backed by the app's XeroApiClient token machinery.

    Refreshes proactively when the stored token is near expiry, and on demand
    after a 401.
    """

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self._client = self._build_client()

    def _build_client(self):
        from apps.xero.xero_auth.models import XeroClientCredentials
        from apps.xero.xero_core.services import XeroApiClient
        credentials = XeroClientCredentials.objects.filter(active=True).first()
        if credentials is None:
            raise CommandError('No active XeroClientCredentials found')
        # XeroApiClient init fetches the tenant token and refreshes if expired
        return XeroApiClient(credentials.user, tenant_id=self.tenant_id)

    def access_token(self):
        from django.utils import timezone
        tenant_token = self._client.tenant_token
        expires_at = tenant_token.expires_at
        if expires_at and expires_at <= timezone.now() + datetime.timedelta(seconds=60):
            self.force_refresh()
            tenant_token = self._client.tenant_token
        token = tenant_token.token or {}
        access_token = token.get('access_token')
        if not access_token:
            raise CommandError(
                f'No access_token available for tenant {self.tenant_id}')
        return access_token

    def force_refresh(self):
        # Rebuilding the client runs the full get_tenant_token path,
        # including the refresh-if-expired logic and DB persistence.
        self._client = self._build_client()
