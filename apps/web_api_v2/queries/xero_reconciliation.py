"""Reconciliation read.

Both sides are already in Postgres, so this query costs no Xero API calls.
"""
from decimal import Decimal

from apps.web_api_v2.services.entity_access import (
    VIEW_FINANCIALS_CAPABILITY,
    capability_codes_for_membership,
)
from apps.web_api_v2.services.fiscal_context import resolve_financial_context
from apps.web_api_v2.services.xero_reconciliation import (
    account_lines,
    STATUS_MATCHED,
    STATUS_MISSING_IN_LEDGER,
    STATUS_MISSING_IN_XERO,
    STATUS_UNCLASSIFIED,
    STATUS_VARIANCE,
    build_reconciliation,
)
from apps.web_api_v2.queries.xero_pipeline import _graphql_error
from apps.web_api_v2.types.xero_reconciliation import (
    ReconciliationAccountClass,
    ReconciliationAccountDetail,
    ReconciliationAccountLine,
    ReconciliationBasis,
    ReconciliationRow,
    ReconciliationStatus,
    ReconciliationSummary,
    XeroReconciliation,
)


def _enum(enum_class, value):
    return enum_class(value) if value else None


def _row(payload):
    return ReconciliationRow(
        account_id=payload['accountId'],
        account_code=payload['accountCode'],
        account_name=payload['accountName'],
        account_class=_enum(ReconciliationAccountClass, payload['accountClass']),
        reporting_group=payload.get('reportingGroup'),
        reporting_line=payload.get('reportingLine'),
        basis=_enum(ReconciliationBasis, payload['basis']),
        xero_value=payload['xeroValue'],
        ledger_value=payload['ledgerValue'],
        variance=payload['variance'],
        status=ReconciliationStatus(payload['status']),
        user_safe_reason=payload['reason'],
    )


def build_xero_reconciliation(info, context_input):
    membership, context = resolve_financial_context(info, context_input)
    if VIEW_FINANCIALS_CAPABILITY not in capability_codes_for_membership(membership):
        _graphql_error(
            info,
            'CAPABILITY_REQUIRED',
            'VIEW_FINANCIALS capability is required.',
        )

    result = build_reconciliation(membership.entity)
    if result is None:
        # An empty reconciliation would read as "everything agrees". Say why
        # there is nothing to show instead.
        return XeroReconciliation(
            context=context,
            available=False,
            user_safe_reason=(
                'No Xero trial balance report has been imported for this entity, '
                'so there is nothing to reconcile against.'
            ),
            report_date=None,
            imported_at=None,
            fiscal_year_start_month=None,
            tolerance=None,
            summary=None,
            rows=[],
        )

    rows = result['rows']
    counts = {status: 0 for status in (
        STATUS_MATCHED, STATUS_VARIANCE, STATUS_MISSING_IN_LEDGER,
        STATUS_MISSING_IN_XERO, STATUS_UNCLASSIFIED,
    )}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1

    return XeroReconciliation(
        context=context,
        available=True,
        user_safe_reason=None,
        report_date=result['reportDate'],
        imported_at=result['importedAt'],
        fiscal_year_start_month=result['fiscalYearStartMonth'],
        tolerance=result['tolerance'],
        summary=ReconciliationSummary(
            accounts_compared=result['accountsCompared'],
            reconciled=result['reconciled'],
            needs_attention=result['needsAttention'],
            net_variance=result['netVariance'] or Decimal('0'),
            # Separately counted so a screen can distinguish a genuine
            # disagreement from a structural asymmetry rather than presenting
            # one undifferentiated "needs attention" number.
            variance_count=counts[STATUS_VARIANCE],
            missing_in_ledger_count=counts[STATUS_MISSING_IN_LEDGER],
            missing_in_xero_count=counts[STATUS_MISSING_IN_XERO],
            unclassified_count=counts[STATUS_UNCLASSIFIED],
        ),
        rows=[_row(payload) for payload in rows],
    )


COMPARISON_NOTE = (
    "Xero's trial balance is an account-level report and publishes no "
    "line-level figure, so these ledger entries have no Xero counterpart to "
    "be compared against individually. They are the evidence behind this "
    "account's side of the variance."
)


def build_xero_reconciliation_account(info, context_input, account_id):
    membership, _context = resolve_financial_context(info, context_input)
    if VIEW_FINANCIALS_CAPABILITY not in capability_codes_for_membership(membership):
        _graphql_error(info, 'CAPABILITY_REQUIRED', 'VIEW_FINANCIALS capability is required.')

    detail = account_lines(membership.entity, account_id)
    if detail is None:
        _graphql_error(
            info, 'NOT_FOUND',
            'That account is not in this entity\'s chart of accounts.',
        )
    return ReconciliationAccountDetail(
        account_id=detail['accountId'],
        account_code=detail['accountCode'],
        account_name=detail['accountName'],
        reporting_group=detail['reportingGroup'],
        reporting_line=detail['reportingLine'],
        truncated=detail['truncated'],
        limit=detail['limit'],
        lines=[ReconciliationAccountLine(
            id=row['id'], date=row['date'], reference=row['reference'],
            description=row['description'], source=row['source'],
            ledger_value=row['ledgerValue'],
        ) for row in detail['lines']],
        comparison_note=COMPARISON_NOTE,
    )
