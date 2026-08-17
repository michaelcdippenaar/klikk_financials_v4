"""
Full refresh of the TM1 'actual' version from Postgres, with a balance check.

Why this exists: the nightly pipeline runs cub.gl_src_trial_balance.import
per AFFECTED PERIOD only, so periods that change in Postgres without a Xero
sync touching them (rebuilds, exclusion-rule edits, backfills) drift in the
cube — at one point TM1 was missing 2015-2016 entirely. A periodic full
refresh (empty pYear/pMonth clears and reloads every period) makes the cube
provably mirror Postgres.

Uses tm1.ExecuteWithReturn, NOT tm1.Execute: the pipeline's execute_process
only checks HTTP status and reports success even when the TI fails.
'HasMinorErrors' is accepted with a warning — it is the known-cosmetic
'Attribute is Not Unique' alias collision (duplicate contact/tracking display
names); cells are written by GUID and no financial data is dropped.

After the load, an MDX check asserts that 'amount' nets to ~0 at All_Entity
across all periods; a non-zero cube fails the command (non-zero exit) so the
cron surfaces it.
"""
import requests
from django.core.management.base import BaseCommand, CommandError

PROCESS = 'cub.gl_src_trial_balance.import'

BALANCE_MDX = (
    "WITH "
    "MEMBER [year].[_ALL] AS 'Aggregate(TM1FILTERBYLEVEL(TM1SUBSETALL([year]),0))' "
    "MEMBER [month].[_ALL] AS 'Aggregate(TM1FILTERBYLEVEL(TM1SUBSETALL([month]),0))' "
    "MEMBER [account].[_ALL] AS 'Aggregate(TM1FILTERBYLEVEL(TM1SUBSETALL([account]),0))' "
    "MEMBER [contact].[_ALL] AS 'Aggregate(TM1FILTERBYLEVEL(TM1SUBSETALL([contact]),0))' "
    "MEMBER [tracking_1].[_ALL] AS 'Aggregate(TM1FILTERBYLEVEL(TM1SUBSETALL([tracking_1]),0))' "
    "MEMBER [tracking_2].[_ALL] AS 'Aggregate(TM1FILTERBYLEVEL(TM1SUBSETALL([tracking_2]),0))' "
    "SELECT {[measure_gl_src_trial_balance].[amount]} ON 0, "
    "{[entity].[All_Entity]} ON 1 FROM [gl_src_trial_balance] "
    "WHERE ([version].[actual],[year].[_ALL],[month].[_ALL],[account].[_ALL],"
    "[contact].[_ALL],[tracking_1].[_ALL],[tracking_2].[_ALL])"
)


class Command(BaseCommand):
    help = "Full refresh of TM1 version 'actual' from Postgres, then verify it balances."

    def add_arguments(self, parser):
        parser.add_argument('--tolerance', type=float, default=1.0,
                            help='Max |All_Entity amount| accepted after load (default 1.0)')
        parser.add_argument('--timeout', type=int, default=1800,
                            help='TI execution timeout in seconds (default 1800)')

    def handle(self, *args, **opts):
        from apps.planning_analytics.services.tm1_client import _resolve_credentials

        base_url, user, password = _resolve_credentials(None, None, None)
        if not base_url:
            raise CommandError('TM1 base URL not configured (TM1ServerConfig / settings)')
        base = base_url.rstrip('/')
        auth = (user, password)

        self.stdout.write(f"Full refresh: {PROCESS} on {base} (empty pYear/pMonth)")
        response = requests.post(
            f"{base}/Processes('{PROCESS}')/tm1.ExecuteWithReturn",
            auth=auth, timeout=opts['timeout'],
            json={'Parameters': [{'Name': 'pYear', 'Value': ''},
                                 {'Name': 'pMonth', 'Value': ''}]},
        )
        if not response.ok:
            raise CommandError(f"TI HTTP {response.status_code}: {response.text[:300]}")
        status = (response.json() or {}).get('ProcessExecuteStatusCode')
        if status == 'CompletedSuccessfully':
            self.stdout.write(f"TI status: {status}")
        elif status == 'HasMinorErrors':
            self.stdout.write(self.style.WARNING(
                f"TI status: {status} (known-cosmetic alias collisions; "
                f"cells write by GUID, no data dropped)"))
        else:
            raise CommandError(f"TI failed: ProcessExecuteStatusCode={status}")

        check = requests.post(
            f"{base}/ExecuteMDX?$expand=Cells($select=Value)",
            auth=auth, json={'MDX': BALANCE_MDX}, timeout=300,
        )
        if not check.ok:
            raise CommandError(f"Balance-check MDX failed: HTTP {check.status_code}")
        cells = check.json().get('Cells', [])
        amount = cells[0].get('Value') if cells else None
        amount = float(amount or 0)
        self.stdout.write(f"All_Entity amount after load: {amount:,.2f}")
        if abs(amount) > opts['tolerance']:
            raise CommandError(
                f"TM1 cube OUT OF BALANCE by {amount:,.2f} "
                f"(tolerance {opts['tolerance']}) - investigate the source data")
        self.stdout.write(self.style.SUCCESS('TM1 full refresh complete and balanced.'))
