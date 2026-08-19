"""
Run the year-end audit registry.

    python manage.py run_yearend_audit --fy 2026
    python manage.py run_yearend_audit --fy 2026 --check BAL-01
    python manage.py run_yearend_audit --fy 2025 --check BAL-01,SUP-05 --json

Read-only: every check is a guarded SELECT executed in a READ ONLY transaction.
Results are stored in audit.check_runs / audit.check_results.
"""
import json

from django.core.management.base import BaseCommand

from apps.audit.services import KLIKK_TENANT_ID, run_audit


class Command(BaseCommand):
    help = 'Run the year-end audit checks for a financial year and store the results.'

    def add_arguments(self, parser):
        parser.add_argument('--fy', type=int, required=True, help='Financial year (the year it ends in), e.g. 2026.')
        parser.add_argument('--check', help='Comma-separated check code(s) to run instead of all active checks.')
        parser.add_argument('--tenant', default=KLIKK_TENANT_ID, help='Xero tenant id (default Klikk).')
        parser.add_argument('--json', action='store_true', help='Print the full JSON result instead of a table.')
        parser.add_argument('--samples', type=int, default=0, help='Print up to N sample rows per non-PASS check.')

    def handle(self, *args, **opts):
        codes = [c.strip() for c in (opts.get('check') or '').split(',') if c.strip()] or None
        out = run_audit(fy=opts['fy'], tenant_id=opts['tenant'], codes=codes, triggered_by='cli:run_yearend_audit')
        if opts['json']:
            self.stdout.write(json.dumps(out, indent=2, default=str))
            return
        s = out['summary']
        self.stdout.write(f"run_id={s['run_id']} fy={s['fy']} ({s['fy_start']}..{s['fy_end']}) tenant={s['tenant_id']}")
        self.stdout.write(f"checks_run={s['checks_run']} PASS={s['counts']['PASS']} WARN={s['counts']['WARN']} "
                          f"FAIL={s['counts']['FAIL']} ERROR={s['counts']['ERROR']}")
        self.stdout.write('')
        hdr = f"{'CODE':8} {'STATUS':6} {'SEV':8} {'ROWS':>6} {'MS':>7}  TITLE"
        self.stdout.write(hdr)
        self.stdout.write('-' * len(hdr))
        for r in out['results']:
            rows = '' if r['row_count'] is None else str(r['row_count'])
            line = f"{r['code']:8} {r['status']:6} {r['severity']:8} {rows:>6} {r['duration_ms']:>7}  {r['title']}"
            style = {'PASS': self.style.SUCCESS, 'WARN': self.style.WARNING, 'FAIL': self.style.ERROR,
                     'ERROR': self.style.NOTICE}[r['status']]
            self.stdout.write(style(line))
            if r['notes'] and r['status'] != 'PASS':
                self.stdout.write(f"{'':8} {r['notes']}")
            if opts['samples'] and r['status'] != 'PASS':
                for row in r['sample_rows'][: opts['samples']]:
                    self.stdout.write(f"{'':10} {json.dumps(row, default=str)[:300]}")
