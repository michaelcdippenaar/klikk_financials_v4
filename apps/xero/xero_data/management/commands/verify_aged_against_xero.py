"""Check locally-computed aged buckets against Xero, for balance-carrying contacts only.

The aged figures are now derived from invoices we already hold, which costs no
API calls. This command is the calibration: it asks Xero for its own aged report
for the contacts that actually carry a balance and reports any disagreement.

It is deliberately a separate, opt-in command rather than part of a sync. It
spends real API budget — one call per contact with a balance, 25 on the Klikk
tenant rather than the several hundred the old per-contact sweep used — so it
runs when someone chooses to spend that, not as a side effect of opening a page.

    manage.py verify_aged_against_xero --tenant <id> --kind payables --limit 10
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.xero.xero_core.exceptions import DailyLimitReached, TenantReauthRequired
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_core.services import serialize_model
from apps.xero.xero_core.throttle import DEFAULT_HEADROOM, HeadroomFloorReached, RateLimitedCaller
from apps.xero.xero_data.aged_from_invoices import BUCKETS, build_aged
from apps.xero.xero_data.aged_reports_service import _extract_buckets, _find_summary_row, _get_api
from apps.xero.xero_data.models import XeroInvoiceType

KINDS = {
    'payables': (XeroInvoiceType.ACCPAY, 'get_report_aged_payables_by_contact'),
    'receivables': (XeroInvoiceType.ACCREC, 'get_report_aged_receivables_by_contact'),
}


class Command(BaseCommand):
    help = "Verify locally-derived aged buckets against Xero's own report."

    def add_arguments(self, parser):
        parser.add_argument('--tenant', required=True)
        parser.add_argument('--kind', choices=sorted(KINDS), default='payables')
        parser.add_argument('--report-date', default=None, help='YYYY-MM-DD; defaults to today')
        parser.add_argument('--limit', type=int, default=None,
                            help='Check at most this many contacts (one API call each).')
        parser.add_argument('--headroom', type=int, default=DEFAULT_HEADROOM)

    def handle(self, *args, **options):
        try:
            tenant = XeroTenant.objects.get(tenant_id=options['tenant'])
        except XeroTenant.DoesNotExist:
            raise CommandError(f"Tenant {options['tenant']} not found")

        report_date = options['report_date'] or timezone.localdate()
        if isinstance(report_date, str):
            report_date = timezone.datetime.strptime(report_date, '%Y-%m-%d').date()

        invoice_type, fetch_name = KINDS[options['kind']]
        local = build_aged(tenant, invoice_type, report_date)
        if not local:
            self.stdout.write(f'No local {options["kind"]} balances at {report_date}; nothing to verify.')
            return

        contacts = list(local)[:options['limit']] if options['limit'] else list(local)
        self.stdout.write(
            f'Verifying {len(contacts)} contact(s) with a balance — '
            f'{len(contacts)} API call(s), not one per contact in the ledger.'
        )

        api, budget_client = _get_api(tenant)
        call = RateLimitedCaller(api_client=budget_client, headroom=options['headroom'])
        fetch = getattr(api, fetch_name)

        agreed = disagreed = 0
        for contact_id in contacts:
            try:
                raw = call(fetch, tenant.tenant_id, contact_id, date=report_date)
            except (DailyLimitReached, HeadroomFloorReached) as exc:
                self.stderr.write(f'Stopped after {call.calls} call(s): {exc}')
                break
            except TenantReauthRequired:
                raise CommandError('Xero tenant needs re-authorization.')

            reports = (serialize_model(raw) or {}).get('Reports') or []
            summary = _find_summary_row(reports[0].get('Rows', [])) if reports else None
            remote = _extract_buckets(summary) if summary else None
            mine = local[contact_id]

            if remote is None:
                # Xero reporting nothing where we hold a balance is itself a
                # disagreement worth naming, not a row to skip quietly.
                disagreed += 1
                self.stdout.write(self.style.WARNING(
                    f'  {mine["contact_name"][:34]:<36} Xero reports no aged rows; '
                    f'local total {mine["total"]}'
                ))
                continue

            differences = [
                (bucket, mine[bucket], Decimal(str(remote.get(bucket, 0))))
                for bucket in BUCKETS
                if abs(mine[bucket] - Decimal(str(remote.get(bucket, 0)))) > Decimal('0.01')
            ]
            if differences:
                disagreed += 1
                self.stdout.write(self.style.WARNING(f'  {mine["contact_name"][:34]:<36} differs:'))
                for bucket, ours, theirs in differences:
                    self.stdout.write(f'      {bucket:<14} local {ours:>14,.2f}   xero {theirs:>14,.2f}')
            else:
                agreed += 1

        self.stdout.write('')
        self.stdout.write(
            f'{agreed} contact(s) agree, {disagreed} differ, {call.calls} API call(s) spent.'
        )
        if disagreed:
            self.stdout.write(self.style.WARNING(
                'Bucket boundaries in aged_from_invoices.bucket_for need adjusting to match Xero.'
            ))
