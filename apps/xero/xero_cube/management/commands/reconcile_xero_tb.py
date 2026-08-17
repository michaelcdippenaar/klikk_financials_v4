"""
Reconcile the app's trial balance against Xero's own Trial Balance report,
per account, through the named bridge:

    Xero report YTD  ==  our TB  +  exclusion add-back  +  year-end-close bridge

Nothing is forced: every rand of difference must land in a named bridge line
or surface as a residual.

Design notes (hard-won, do not "simplify" away):
  * Accounts are matched by ACCOUNT ID from the report row attributes, never
    by name — report names differ from chart names (e.g. "Anchor 0426478" vs
    "Anchor 0426478 (WEIB36470) - Investment").
  * Xero's TB report shows P&L accounts as FINANCIAL-YEAR-TO-DATE, with prior
    years closed into Retained Earnings. Our TB is cumulative-never-closed.
    So P&L accounts are compared FY-scoped, and the sum of prior-FY P&L
    (ours + exclusions) is bridged onto the Retained Earnings account.
  * The exclusion add-back uses the SAME matcher as the TB build (whole
    journals when journal_id is set), so a rule can never half-apply here.

Usage:
    python manage.py reconcile_xero_tb --tenant <tenant_id> [--date YYYY-MM-DD]
                                       [--tolerance 0.01] [--limit 25]
"""
from datetime import date as date_cls, datetime
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

# P&L account types close into Retained Earnings at Xero's year end.
PNL_TYPES = ('REVENUE', 'EXPENSE', 'OTHERINCOME', 'OVERHEADS', 'DIRECTCOSTS', 'SALES')

# Must stay in lockstep with XeroTrailBalanceManager.consolidate_journals.
EXCLUDED_MATCH = """
EXISTS (
    SELECT 1 FROM xero_data_xerojournalexclusion ex
    WHERE ex.active = TRUE
      AND ex.organisation_id = j.organisation_id
      AND (ex.journal_type = '' OR ex.journal_type = j.journal_type)
      AND (ex.journal_number IS NULL OR ex.journal_number = j.journal_number)
      AND (ex.journal_id = ''
           OR regexp_replace(ex.journal_id, '_[0-9]+$', '')
              = regexp_replace(j.journal_id, '_[0-9]+$', ''))
      AND (ex.date IS NULL OR ex.date = j.date::date)
      AND (ex.journal_id <> '' OR ex.description = '' OR ex.description = j.description)
      AND (ex.journal_id <> '' OR ex.reference = '' OR ex.reference = j.reference)
)
"""


def _q(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params or [])
        return cursor.fetchall()


class Command(BaseCommand):
    help = "Reconcile the app trial balance to Xero's Trial Balance report, per account."

    def add_arguments(self, parser):
        parser.add_argument('--tenant', required=True, help='Xero tenant ID')
        parser.add_argument('--date', default=None,
                            help='As-at date YYYY-MM-DD (default: today)')
        parser.add_argument('--tolerance', type=float, default=0.01,
                            help='Absolute residual treated as reconciled (default 0.01)')
        parser.add_argument('--limit', type=int, default=25,
                            help='Max residual rows to print (default 25)')
        parser.add_argument('--all-years', action='store_true',
                            help='Compare at every financial-year end, not just --date')
        parser.add_argument('--export', default=None,
                            help='Write the comparison to this .xlsx (or .csv) path')

    # ------------------------------------------------------------------ Xero
    def _fetch_xero_tb(self, tenant, as_at):
        """Return {account_id: ytd_balance} from Xero's Trial Balance report."""
        from apps.xero.xero_auth.models import XeroClientCredentials
        from apps.xero.xero_core.services import (
            XeroApiClient, XeroAccountingApi, serialize_model,
        )
        credentials = XeroClientCredentials.objects.filter(active=True).first()
        if not credentials:
            raise CommandError('No active Xero credentials')
        wrapper = XeroAccountingApi(
            XeroApiClient(credentials.user, tenant_id=tenant.tenant_id),
            tenant.tenant_id,
        )
        report = serialize_model(
            wrapper.api_client.get_report_trial_balance(tenant.tenant_id, date=as_at)
        )
        balances = {}
        for rep in report.get('Reports', []):
            for section in rep.get('Rows', []):
                for row in section.get('Rows') or []:
                    cells = row.get('Cells') or []
                    if len(cells) < 5:
                        continue
                    account_id = None
                    for attr in (cells[0].get('Attributes') or []):
                        if attr.get('Value'):
                            account_id = attr['Value']
                            break
                    if not account_id:
                        continue
                    try:
                        ytd_debit = float(cells[3].get('Value') or 0)
                        ytd_credit = float(cells[4].get('Value') or 0)
                    except (TypeError, ValueError):
                        continue
                    balances[account_id] = balances.get(account_id, 0.0) + ytd_debit - ytd_credit
        return balances

    # ------------------------------------------------------------------ ours
    def _our_balances(self, tenant_id, date_from, date_to):
        """{account_id: sum(amount)} from TB-eligible journals in [from, to]."""
        rows = _q(f"""
            SELECT j.account_id, SUM(j.amount)
            FROM xero_data_xerojournals j
            WHERE j.organisation_id = %s
              AND j.journal_type <> 'journal'
              AND j.date >= %s AND j.date <= %s
              AND NOT {EXCLUDED_MATCH}
            GROUP BY j.account_id
        """, [tenant_id, date_from, date_to])
        return {a: float(v) for a, v in rows}

    def _excluded_balances(self, tenant_id, date_from, date_to):
        rows = _q(f"""
            SELECT j.account_id, SUM(j.amount)
            FROM xero_data_xerojournals j
            WHERE j.organisation_id = %s
              AND j.journal_type <> 'journal'
              AND j.date >= %s AND j.date <= %s
              AND {EXCLUDED_MATCH}
            GROUP BY j.account_id
        """, [tenant_id, date_from, date_to])
        return {a: float(v) for a, v in rows}

    # ------------------------------------------------------------- all years
    def _bridged(self, tenant, accounts, retained, fiscal_start, as_at):
        """Per-account (xero, ours+excl+close) at one as-at date."""
        epoch = date_cls(1900, 1, 1)
        fy_start = (date_cls(as_at.year, fiscal_start, 1)
                    if as_at.month >= fiscal_start
                    else date_cls(as_at.year - 1, fiscal_start, 1))
        xero = self._fetch_xero_tb(tenant, as_at)
        ours_all = self._our_balances(tenant.tenant_id, epoch, as_at)
        ours_fy = self._our_balances(tenant.tenant_id, fy_start, as_at)
        excl_all = self._excluded_balances(tenant.tenant_id, epoch, as_at)
        excl_fy = self._excluded_balances(tenant.tenant_id, fy_start, as_at)
        close = sum(
            ours_all.get(a, 0.0) - ours_fy.get(a, 0.0)
            + excl_all.get(a, 0.0) - excl_fy.get(a, 0.0)
            for a, acc in accounts.items() if acc.type in PNL_TYPES)
        out = {}
        for account_id in set(list(xero) + list(ours_all)):
            acc = accounts.get(account_id)
            is_pnl = bool(acc and acc.type in PNL_TYPES)
            ours = (ours_fy if is_pnl else ours_all).get(account_id, 0.0) \
                + (excl_fy if is_pnl else excl_all).get(account_id, 0.0)
            if retained and account_id == retained.account_id:
                ours += close
            out[account_id] = (xero.get(account_id, 0.0), ours)
        return out

    def _run_all_years(self, tenant, accounts, retained, fiscal_start, as_at, export):
        from datetime import timedelta
        first = _q("""SELECT MIN(date) FROM xero_data_xerojournals
                      WHERE organisation_id=%s AND journal_type <> 'journal'""",
                   [tenant.tenant_id])[0][0]
        first_year = first.year + (1 if first.month >= fiscal_start else 0)
        ends, y = [], first_year
        while True:
            end = (date_cls(y, fiscal_start, 1) - timedelta(days=1)
                   if fiscal_start > 1 else date_cls(y, 12, 31))
            if end >= as_at:
                break
            ends.append((f"FY{y}", end))
            y += 1
        ends.append((f"FY{y} (to {as_at})", as_at))

        per_year, names = {}, {}
        for label, end in ends:
            self.stdout.write(f"  fetching {label} ({end}) ...")
            per_year[label] = self._bridged(tenant, accounts, retained,
                                            fiscal_start, end)
        all_ids = sorted(
            {a for d in per_year.values() for a in d},
            key=lambda a: (accounts[a].type if a in accounts else 'z',
                           accounts[a].name if a in accounts else a))
        header = ['account', 'type']
        for label, _ in ends:
            header += [f'{label} Xero', f'{label} ours', f'{label} diff']
        rows = []
        for account_id in all_ids:
            acc = accounts.get(account_id)
            row = [acc.name if acc else account_id, acc.type if acc else '?']
            for label, _ in ends:
                xv, ov = per_year[label].get(account_id, (0.0, 0.0))
                row += [round(xv, 2), round(ov, 2), round(ov - xv, 2)]
            rows.append(row)

        out_path = export or f'/tmp/xero_recon_{tenant.tenant_name.replace(" ", "_")}.xlsx'
        try:
            import pandas as pd
            df = pd.DataFrame(rows, columns=header)
            if out_path.endswith('.csv'):
                df.to_csv(out_path, index=False)
            else:
                df.to_excel(out_path, index=False, sheet_name=tenant.tenant_name[:31])
        except Exception as exc:  # openpyxl missing etc. — never lose the data
            out_path = out_path.rsplit('.', 1)[0] + '.csv'
            import csv
            with open(out_path, 'w', newline='') as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(rows)
            self.stdout.write(f"  (xlsx failed: {exc}; wrote CSV instead)")
        bad = sum(1 for r in rows if any(abs(r[i]) > 0.01
                  for i in range(4, len(r), 3)))
        self.stdout.write(
            f"Exported {len(rows)} accounts x {len(ends)} years -> {out_path}"
            f"  ({bad} accounts differ somewhere)")
        return None

    # ------------------------------------------------------------------ main
    def handle(self, *args, **opts):
        from apps.xero.xero_core.models import XeroTenant
        from apps.xero.xero_metadata.models import XeroAccount

        try:
            tenant = XeroTenant.objects.get(tenant_id=opts['tenant'])
        except XeroTenant.DoesNotExist:
            raise CommandError(f"Tenant {opts['tenant']} not found")

        as_at = (datetime.strptime(opts['date'], '%Y-%m-%d').date()
                 if opts['date'] else date_cls.today())
        fiscal_start = tenant.get_fiscal_year_start_month() or 1
        fy_start = (date_cls(as_at.year, fiscal_start, 1)
                    if as_at.month >= fiscal_start
                    else date_cls(as_at.year - 1, fiscal_start, 1))
        epoch = date_cls(1900, 1, 1)
        tol = Decimal(str(opts['tolerance']))

        accounts = {
            a.account_id: a for a in XeroAccount.objects.filter(organisation=tenant)
        }
        retained = next(
            (a for a in accounts.values()
             if (a.reporting_code or '').startswith('EQU.RET')
             or 'retained earnings' in (a.name or '').lower()),
            None,
        )

        self.stdout.write(
            f"Reconciling {tenant.tenant_name} to Xero TB report as at {as_at} "
            f"(FY starts {fy_start})")

        if opts['all_years']:
            return self._run_all_years(tenant, accounts, retained, fiscal_start,
                                       as_at, opts['export'])

        xero = self._fetch_xero_tb(tenant, as_at)
        ours_all = self._our_balances(tenant.tenant_id, epoch, as_at)
        ours_fy = self._our_balances(tenant.tenant_id, fy_start, as_at)
        excl_all = self._excluded_balances(tenant.tenant_id, epoch, as_at)
        excl_fy = self._excluded_balances(tenant.tenant_id, fy_start, as_at)

        # Prior-FY P&L (ours + exclusions) closes into Retained Earnings.
        close_bridge = 0.0
        for account_id, account in accounts.items():
            if account.type in PNL_TYPES:
                close_bridge += (
                    ours_all.get(account_id, 0.0) - ours_fy.get(account_id, 0.0)
                    + excl_all.get(account_id, 0.0) - excl_fy.get(account_id, 0.0)
                )

        residuals = []
        reconciled = 0
        for account_id in set(list(xero) + list(ours_all)):
            account = accounts.get(account_id)
            name = account.name if account else f'(unknown {account_id})'
            is_pnl = bool(account and account.type in PNL_TYPES)
            ours = ours_fy.get(account_id, 0.0) if is_pnl else ours_all.get(account_id, 0.0)
            excl = excl_fy.get(account_id, 0.0) if is_pnl else excl_all.get(account_id, 0.0)
            bridge = close_bridge if (retained and account_id == retained.account_id) else 0.0
            residual = ours + excl + bridge - xero.get(account_id, 0.0)
            if abs(residual) <= float(tol):
                reconciled += 1
            else:
                residuals.append((abs(residual), name,
                                  account.type if account else '?',
                                  xero.get(account_id, 0.0), ours, excl, bridge, residual))

        residuals.sort(reverse=True)
        total = len(set(list(xero) + list(ours_all)))
        self.stdout.write(
            f"\n{reconciled}/{total} accounts reconcile within {tol}; "
            f"{len(residuals)} residuals (close bridge onto "
            f"{retained.name if retained else 'NO RE ACCOUNT FOUND'}: {close_bridge:,.2f})")
        if residuals:
            self.stdout.write(
                f"\n{'account':<40}{'type':<11}{'Xero':>15}{'ours':>15}"
                f"{'excl':>12}{'close':>14}{'RESIDUAL':>14}")
            for _, name, atype, xv, ov, ev, bv, res in residuals[:opts['limit']]:
                self.stdout.write(
                    f"{name[:38]:<40}{str(atype)[:9]:<11}{xv:>15,.2f}{ov:>15,.2f}"
                    f"{ev:>12,.2f}{bv:>14,.2f}{res:>14,.2f}")
            if len(residuals) > opts['limit']:
                self.stdout.write(f"... and {len(residuals) - opts['limit']} more")
        net_residual = sum(r[-1] for r in residuals)
        self.stdout.write(f"\nNet residual: {net_residual:,.2f}")
        return None
