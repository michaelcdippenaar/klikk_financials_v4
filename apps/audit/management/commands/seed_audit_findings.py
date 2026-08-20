"""
Seed the FY2026 audit findings register (the 10 findings open at 2026-08-20).

    python manage.py seed_audit_findings            # create missing / update existing
    python manage.py seed_audit_findings --dry-run  # print what would happen, write nothing

Idempotent on ``(fy, title)``: an existing row is updated in place and KEEPS
its ref (never re-allocated) and its workflow ``status`` (mirrors
``seed_audit_checks``, where a deactivated check stays deactivated — a finding
MC has resolved must not snap back to OPEN on a reseed). New rows get the next
ref via ``allocate_ref`` under the advisory lock.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.audit.findings_services import allocate_ref
from apps.audit.models import AuditFinding

DEFAULT_SOURCE = 'seed_audit_findings 2026-08-20'
FY = 2026

# amount=None where unquantified; asana_gid='' where no task exists yet.
FINDINGS = [
    {
        'severity': 'HIGH', 'category': 'SUP',
        'title': 'Payments made before supplier bill captured',
        'amount': Decimal('429110.39'), 'owner': 'bookkeeper',
        'source': 'internal-audit run 13', 'asana_gid': '1217633700114593',
        'description': (
            'Supplier payments should only leave the bank once the underlying bill has been '
            'captured and approved. R429,110.39 of FY2026 payments cleared the bank before any '
            'corresponding supplier bill existed in the ledger, so the spend bypassed the '
            'purchase-approval trail and the creditors ledger understates what was actually '
            'committed at the time of payment.'
        ),
    },
    {
        'severity': 'HIGH', 'category': 'PRC',
        'title': 'SARS banking-details verification — Lia Dippenaar (related party)',
        'amount': None, 'owner': 'MC',
        'source': 'internal-audit run 13', 'asana_gid': '1217628653069456',
        'description': (
            'SARS requires that banking details on the taxpayer profile be verified and current. '
            'The banking-details verification for Lia Dippenaar, a related party, is outstanding. '
            'Until it is completed, SARS refunds and correspondence for that profile are at risk '
            'of delay or misdirection, and the related-party status makes the gap a governance '
            'as well as an administrative issue.'
        ),
    },
    {
        'severity': 'LOW', 'category': 'SUP',
        'title': 'Higgsfield R3,166.42 charged twice on card',
        'amount': Decimal('3166.42'), 'owner': 'bookkeeper',
        'source': 'internal-audit run 13', 'asana_gid': '1217633700114529',
        'description': (
            'Each card charge should appear once in the books and once on the statement. '
            'Higgsfield charged R3,166.42 twice on the company card, so expenses are overstated '
            'by one occurrence of the charge unless the duplicate is refunded by the supplier or '
            'reversed in the ledger. A refund request or chargeback should be pursued and the '
            'books corrected to a single charge.'
        ),
    },
    {
        'severity': 'HIGH', 'category': 'VAT',
        'title': 'Input VAT recoverable ~R45,644 (bills captured VAT-inclusive, no split)',
        'amount': Decimal('45644.00'), 'owner': 'accountant',
        'source': 'input-VAT audit 2026-08-19', 'asana_gid': '1217649237470889',
        'description': (
            'VAT-registered purchases must be captured with the VAT portion split out so the '
            'input tax can be claimed. A set of supplier bills was captured VAT-inclusive with no '
            'split, leaving roughly R45,644.00 of input VAT unclaimed. Expenses are overstated by '
            'the same amount and the VAT returns for the affected periods under-claim; the bills '
            'need recapture or adjustment before the claim window closes.'
        ),
    },
    {
        'severity': 'HIGH', 'category': 'PAYROLL',
        'title': 'Wandeli R99,810 paid as supplier — no payroll/UIF/SDL; worker-vs-contractor classification',
        'amount': Decimal('99810.00'), 'owner': 'accountant',
        'source': 'codex worker audit 2026-08-20', 'asana_gid': '',
        'description': (
            'Payments to workers must run through payroll with PAYE, UIF and SDL accounted for '
            'unless the payee is genuinely an independent contractor. Wandeli was paid '
            'R99,810.00 as a supplier with no payroll processing at all, so the '
            'worker-vs-contractor classification is unresolved. If the relationship is employment, '
            'the company carries exposure for the payroll taxes and levies that were never '
            'withheld or paid over.'
        ),
    },
    {
        'severity': 'HIGH', 'category': 'PAYROLL',
        'title': 'R17,420.01 routed through Joice for dayworkers/overtime — no payee detail or property tracking',
        'amount': Decimal('17420.01'), 'owner': 'MC',
        'source': 'codex worker audit 2026-08-20', 'asana_gid': '',
        'description': (
            'Payments to casual labour should identify who was paid, for what work, and against '
            'which property, so the cost can be allocated and the payroll obligations assessed. '
            'R17,420.01 was routed through Joice as a single conduit for dayworkers and overtime '
            'with no underlying payee detail or property tracking, leaving the spend unallocable, '
            'the recipients unidentifiable, and any employee-tax obligations on those payments '
            'unassessed.'
        ),
    },
    {
        'severity': 'MEDIUM', 'category': 'PAYROLL',
        'title': 'R4,820 Wandeli payments from MC personal account — company purpose + director loan decision',
        'amount': Decimal('4820.00'), 'owner': 'MC',
        'source': 'codex worker audit 2026-08-20', 'asana_gid': '',
        'description': (
            'Company expenses should be paid from company accounts, or, when paid personally, '
            'recognised in the books with a matching director-loan movement. R4,820.00 was paid '
            'to Wandeli from MC\'s personal account for what appears to be company work; the '
            'company purpose needs confirming and a decision is required on booking the expense '
            'with a credit to the director loan account, failing which the books omit a real cost.'
        ),
    },
    {
        'severity': 'MEDIUM', 'category': 'BNK', 'check_code': 'BNK-05',
        'title': 'R458,498 of payments coded to loan accounts though cash left a Klikk bank account (BNK-05 MISPOSTED)',
        'amount': Decimal('458498.00'), 'owner': 'bookkeeper',
        'source': 'internal-audit run 13', 'asana_gid': '1217632488924819',
        'description': (
            'A payment made from a company bank account is company spend and should be coded to '
            'the relevant expense or asset account, not to a shareholder/director loan account. '
            'R458,498.00 of payments were coded to loan accounts even though the cash '
            'demonstrably left a Klikk bank account (check BNK-05, MISPOSTED). The loan-account '
            'balances are misstated and the expense base is understated until each payment is '
            'recoded to what it actually paid for.'
        ),
    },
    {
        'severity': 'MEDIUM', 'category': 'DOC', 'check_code': 'DOC-03',
        'title': 'R579k FY2026 spend with no attachment and no slip (DOC-03)',
        'amount': Decimal('579160.00'), 'owner': 'bookkeeper',
        'source': 'internal-audit run 13', 'asana_gid': '',
        'description': (
            'Every material payment should carry supporting documentation — an attached invoice '
            'in Xero or a slip in the Slippies register. R579,160.00 of FY2026 spend has neither '
            '(check DOC-03), so those transactions cannot be vouched, deductions and VAT claims '
            'on them are unsupported, and the missing documents need to be sourced from suppliers '
            'or the intake channels before year-end.'
        ),
    },
    {
        'severity': 'HIGH', 'category': 'SUP',
        'title': 'Aurras R138,000 paid with no invoice (R64,400 personal + R73,600)',
        'amount': Decimal('138000.00'), 'owner': 'MC',
        'source': 'internal-audit run 13', 'asana_gid': '1217591235585694',
        'description': (
            'Supplier payments require an invoice before they are made and to support the expense '
            'afterwards. Aurras was paid R138,000.00 in total — R64,400.00 of it from a personal '
            'account and R73,600.00 — with no invoice held for any of it. The spend is currently '
            'unvouchable, the personal portion needs a director-loan treatment decision, and the '
            'invoices must be obtained from the supplier before the expense can be supported.'
        ),
    },
]


class Command(BaseCommand):
    help = 'Seed/refresh the 10 FY2026 audit findings (idempotent on (fy, title)).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Print what would happen; write nothing.')

    def handle(self, *args, **opts):
        dry_run = opts.get('dry_run')
        created = updated = 0
        for spec in FINDINGS:
            defaults = {
                'severity': spec['severity'],
                'category': spec['category'],
                'amount': spec['amount'],
                'currency': 'ZAR',
                'description': spec['description'],
                'owner': spec['owner'],
                'source': spec.get('source') or DEFAULT_SOURCE,
                'check_code': spec.get('check_code', ''),
                'asana_gid': spec.get('asana_gid', ''),
            }
            existing = AuditFinding.objects.filter(fy=FY, title=spec['title']).first()
            if existing is not None:
                if dry_run:
                    self.stdout.write(f'would update {existing.ref}: {spec["title"]}')
                else:
                    for field, value in defaults.items():
                        setattr(existing, field, value)
                    existing.updated_by = 'seed'
                    existing.save(update_fields=[*defaults, 'updated_by', 'updated_at'])
                    self.stdout.write(f'updated {existing.ref}: {spec["title"]}')
                updated += 1
                continue
            if dry_run:
                self.stdout.write(f'would create [{spec["severity"]}/{spec["category"]}]: {spec["title"]}')
                created += 1
                continue
            with transaction.atomic():
                finding = AuditFinding.objects.create(
                    fy=FY,
                    ref=allocate_ref(FY),
                    title=spec['title'],
                    status='OPEN',
                    evidence=[],
                    created_by='seed',
                    **defaults,
                )
            self.stdout.write(f'created {finding.ref}: {finding.title}')
            created += 1
        verb = 'would seed' if dry_run else 'seeded'
        self.stdout.write(self.style.SUCCESS(f'{verb}: created={created} updated={updated}'))
