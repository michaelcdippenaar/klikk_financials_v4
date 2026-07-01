"""Generate the forward 13-week cash-flow forecast runs into CashflowForecast.

DRY-RUN BY DEFAULT — computes and prints the per-scenario row counts and writes
nothing. Pass --commit to persist. A regenerate REPLACES prior rows for the same
(run_id, scenario); source bank data is never touched.

    python manage.py generate_cashflow_forecast                       # dry-run, all scenarios
    python manage.py generate_cashflow_forecast --commit
    python manage.py generate_cashflow_forecast --anchor 2026-06-25 --commit
    python manage.py generate_cashflow_forecast --scenario base --commit
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.investec.cashflow_forecast import (
    SCENARIOS_AVAILABLE,
    generate_cashflow_forecast,
    get_cashflow_forecast,
)


class Command(BaseCommand):
    help = "Generate the 13-week cash-flow forecast runs. Dry-run unless --commit."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Persist (default: dry-run).")
        parser.add_argument("--anchor", help="Anchor date YYYY-MM-DD (default: today).")
        parser.add_argument("--weeks", type=int, default=13)
        parser.add_argument("--scenario", help="Single scenario (default: all).")
        parser.add_argument("--run-id", help="Explicit run_id (default: cf-<anchor>).")

    def handle(self, *args, **o):
        anchor = None
        if o["anchor"]:
            try:
                anchor = date.fromisoformat(o["anchor"])
            except ValueError:
                raise CommandError(f"Invalid date: {o['anchor']!r} (use YYYY-MM-DD).")

        scenarios = None
        if o["scenario"]:
            if o["scenario"] not in SCENARIOS_AVAILABLE:
                raise CommandError(f"Unknown scenario {o['scenario']!r}. Available: {SCENARIOS_AVAILABLE}")
            scenarios = [o["scenario"]]

        commit = o["commit"]
        summary = generate_cashflow_forecast(
            anchor_date=anchor, weeks=o["weeks"], scenarios=scenarios,
            run_id=o["run_id"], persist=commit,
        )
        mode = "COMMIT" if commit else "DRY-RUN"
        self.stdout.write(self.style.WARNING(
            f"[{mode}] run_id={summary['run_id']}  anchor={summary['anchor_date']}  weeks={summary['weeks']}"
        ))
        for sc, s in summary["scenarios"].items():
            # Re-run the accessor to surface the low-point for the operator.
            r = get_cashflow_forecast(anchor_date=anchor, weeks=o["weeks"], scenario=sc)
            lp = r["low_point"]
            self.stdout.write(
                f"    {sc:18} rows={s['rows']:>4}  opening={r['opening_balance']:>14,.2f}  "
                f"low={lp['amount']:>14,.2f} (W{lp['week']})"
            )
        if not commit:
            self.stdout.write(self.style.NOTICE("  dry-run only — re-run with --commit to persist."))
