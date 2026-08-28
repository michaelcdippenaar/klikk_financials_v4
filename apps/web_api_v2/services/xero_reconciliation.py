"""Trial-balance reconciliation: Xero's own report against our ledger.

Two genuinely independent sides, both already in Postgres, so building this
costs **no Xero API calls**:

  - ``XeroTrailBalanceReport`` / ``...ReportLine`` — Xero's Trial Balance report
    as Xero computed it, imported and parsed.
  - ``XeroTrailBalance`` — our own consolidation of ``XeroJournals`` into
    account/period buckets.

A variance therefore means our ledger and Xero's report disagree, which is
exactly what this screen exists to surface.

THE COMPARISON BASIS DEPENDS ON THE ACCOUNT CLASS, and getting this wrong is
what made the V1 comparison unusable. Measured on Klikk's 2026-08-31 report:

    single month for everything (V1)        3 match / 125 mismatch   R313.1m
    fiscal YTD for everything              31 match /  97 mismatch   R313.3m
    per class, as implemented here         93 match /  35 mismatch    R19.4m

Profit-and-loss accounts reset at the start of each fiscal year, so Xero's YTD
column must be met with a fiscal-year-to-date sum. Balance-sheet accounts carry
forward from inception, so they must be met with an inception-to-date sum. V1
compared everything against a single calendar month, which is neither.

The account class comes from Xero's own ``Class`` on the account payload rather
than a hand-written mapping of account types. An account we cannot classify is
reported as UNCLASSIFIED with a typed reason — never silently bucketed, because
a wrong basis produces a confident, wrong variance.
"""
from decimal import Decimal

from django.db.models import Q, Sum

from apps.xero.xero_cube.models import XeroTrailBalance
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_validation.models import (
    XeroTrailBalanceReport,
    XeroTrailBalanceReportLine,
)

# Cent tolerance. Both sides descend from the same Xero data, so anything above
# rounding is a real disagreement; materiality ranks it, it does not excuse it.
DEFAULT_TOLERANCE = Decimal('0.01')

PROFIT_AND_LOSS_CLASSES = frozenset({'REVENUE', 'EXPENSE'})
BALANCE_SHEET_CLASSES = frozenset({'ASSET', 'LIABILITY', 'EQUITY'})

# Fallback only, for accounts whose payload predates the Class field.
_TYPE_TO_CLASS = {
    'REVENUE': 'REVENUE', 'OTHERINCOME': 'REVENUE', 'SALES': 'REVENUE',
    'EXPENSE': 'EXPENSE', 'OVERHEADS': 'EXPENSE', 'DIRECTCOSTS': 'EXPENSE',
    'BANK': 'ASSET', 'CURRENT': 'ASSET', 'FIXED': 'ASSET',
    'INVENTORY': 'ASSET', 'NONCURRENT': 'ASSET', 'PREPAYMENT': 'ASSET',
    'CURRLIAB': 'LIABILITY', 'LIABILITY': 'LIABILITY', 'TERMLIAB': 'LIABILITY',
    'EQUITY': 'EQUITY',
}

BASIS_FISCAL_YTD = 'FISCAL_YEAR_TO_DATE'
BASIS_INCEPTION = 'INCEPTION_TO_DATE'

STATUS_MATCHED = 'MATCHED'
STATUS_VARIANCE = 'VARIANCE'
STATUS_MISSING_IN_LEDGER = 'MISSING_IN_LEDGER'
STATUS_MISSING_IN_XERO = 'MISSING_IN_XERO'
STATUS_UNCLASSIFIED = 'UNCLASSIFIED'


def account_class(account):
    """Xero's own Class for an account, or None when it cannot be determined."""
    collection = getattr(account, 'collection', None) or {}
    raw = collection.get('Class') or collection.get('class')
    if raw:
        value = str(raw).strip().upper()
        if value in PROFIT_AND_LOSS_CLASSES or value in BALANCE_SHEET_CLASSES:
            return value
    return _TYPE_TO_CLASS.get(str(getattr(account, 'type', '') or '').strip().upper())


def comparison_basis(klass):
    if klass in PROFIT_AND_LOSS_CLASSES:
        return BASIS_FISCAL_YTD
    if klass in BALANCE_SHEET_CLASSES:
        return BASIS_INCEPTION
    return None


def latest_report(entity, report_date=None):
    reports = XeroTrailBalanceReport.objects.filter(organisation=entity)
    if report_date:
        reports = reports.filter(report_date=report_date)
    return reports.order_by('-report_date', '-imported_at').first()


def _ledger_totals(entity, condition):
    return {
        row['account']: Decimal(str(row['total'] or 0))
        for row in XeroTrailBalance.objects
        .filter(organisation=entity)
        .filter(condition)
        .values('account')
        .annotate(total=Sum('amount'))
    }


def _fiscal_year_to_date(year, month, fiscal_start_month):
    """Periods from the fiscal-year start up to and including the report month."""
    if fiscal_start_month > month:
        # The fiscal year opened in the previous calendar year.
        return Q(year=year - 1, month__gte=fiscal_start_month) | Q(year=year, month__lte=month)
    return Q(year=year, month__gte=fiscal_start_month, month__lte=month)


def _inception_to_date(year, month):
    return Q(year__lt=year) | Q(year=year, month__lte=month)


def build_reconciliation(entity, report_date=None, tolerance=DEFAULT_TOLERANCE):
    """Reconcile Xero's trial balance against our ledger. No Xero API calls.

    Returns None when the entity has no imported report — the caller reports
    that as its own typed reason rather than showing an empty reconciliation,
    which would read as "everything agrees".
    """
    report = latest_report(entity, report_date)
    if report is None:
        return None

    lines = list(
        XeroTrailBalanceReportLine.objects
        .filter(report=report, row_type__in=['Row', None, ''])
        .select_related('account')
    )

    year, month = report.report_date.year, report.report_date.month
    fiscal_start = entity.get_fiscal_year_start_month()
    ledger = {
        BASIS_FISCAL_YTD: _ledger_totals(entity, _fiscal_year_to_date(year, month, fiscal_start)),
        BASIS_INCEPTION: _ledger_totals(entity, _inception_to_date(year, month)),
    }

    rows = []
    seen_accounts = set()
    for line in lines:
        if line.account is None:
            # Xero reported an account our metadata does not know. Naming it is
            # the useful answer; pretending it reconciles is not.
            rows.append({
                'accountId': None,
                'accountCode': line.account_code or '',
                'accountName': line.account_name or '',
                'accountClass': None,
                'basis': None,
                'xeroValue': line.value,
                'ledgerValue': None,
                'variance': None,
                'status': STATUS_MISSING_IN_LEDGER,
                'reason': (
                    f'Xero reports account {line.account_code or "(no code)"} '
                    f'which is not in the local chart of accounts.'
                ),
            })
            continue

        seen_accounts.add(line.account.account_id)
        klass = account_class(line.account)
        basis = comparison_basis(klass)
        if basis is None:
            rows.append({
                'accountId': line.account.account_id,
                'accountCode': line.account_code or '',
                'accountName': line.account_name or '',
                'accountClass': klass,
                'basis': None,
                'xeroValue': line.value,
                'ledgerValue': None,
                'variance': None,
                'status': STATUS_UNCLASSIFIED,
                'reason': (
                    'This account has no Xero class, so the comparison basis is '
                    'unknown. Comparing it on a guessed basis would report a '
                    'confident, wrong variance.'
                ),
            })
            continue

        ledger_value = ledger[basis].get(line.account.account_id, Decimal('0'))
        variance = line.value - ledger_value
        rows.append({
            'accountId': line.account.account_id,
            'accountCode': line.account_code or '',
            'accountName': line.account_name or '',
            'accountClass': klass,
            'basis': basis,
            'xeroValue': line.value,
            'ledgerValue': ledger_value,
            'variance': variance,
            'status': STATUS_MATCHED if abs(variance) <= tolerance else STATUS_VARIANCE,
            'reason': None,
        })

    rows.extend(_ledger_only_rows(entity, ledger, seen_accounts, tolerance))
    rows.sort(key=lambda row: (abs(row['variance'] or 0)), reverse=True)

    compared = sum(1 for row in rows if row['status'] in (STATUS_MATCHED, STATUS_VARIANCE))
    reconciled = sum(1 for row in rows if row['status'] == STATUS_MATCHED)
    return {
        'reportId': report.pk,
        'reportDate': report.report_date,
        'importedAt': report.imported_at,
        'fiscalYearStartMonth': fiscal_start,
        'tolerance': tolerance,
        'accountsCompared': compared,
        'reconciled': reconciled,
        'needsAttention': sum(1 for row in rows if row['status'] != STATUS_MATCHED),
        'netVariance': sum((row['variance'] or Decimal('0')) for row in rows),
        'rows': rows,
    }


def _ledger_only_rows(entity, ledger, seen_accounts, tolerance):
    """Accounts carrying a ledger balance that Xero's report never mentioned.

    Reported on the balance-sheet basis: a P&L account absent from Xero's report
    for the period is ordinary, whereas a balance carried in our ledger and
    absent from Xero's is the asymmetry worth showing.
    """
    candidates = {
        account_id: value
        for account_id, value in ledger[BASIS_INCEPTION].items()
        if account_id not in seen_accounts and abs(value) > tolerance
    }
    if not candidates:
        return []

    accounts = {
        account.account_id: account
        for account in XeroAccount.objects.filter(
            organisation=entity, account_id__in=list(candidates)
        )
    }
    rows = []
    for account_id, value in candidates.items():
        account = accounts.get(account_id)
        if account is None:
            continue
        rows.append({
            'accountId': account_id,
            'accountCode': account.code or '',
            'accountName': account.name or '',
            'accountClass': account_class(account),
            'basis': BASIS_INCEPTION,
            'xeroValue': Decimal('0'),
            'ledgerValue': value,
            'variance': -value,
            'status': STATUS_MISSING_IN_XERO,
            'reason': 'The local ledger holds a balance that Xero\'s report does not report.',
        })
    return rows
