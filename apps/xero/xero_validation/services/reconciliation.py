"""
Reconciliation service: fetch P&L and Balance Sheet (via Xero reports), compare to our trail balance, per financial year.
"""
import calendar
import logging
from datetime import date
from decimal import Decimal

from apps.xero.xero_core.models import XeroTenant

from .imports import import_profit_loss_from_xero_reconciliation, import_trail_balance_from_xero
from .comparisons import compare_profit_loss
from .validation import validate_balance_sheet_accounts

logger = logging.getLogger(__name__)


def _financial_year_dates(financial_year, fiscal_year_start_month):
    """
    Return (from_date, to_date) for the given financial year.
    E.g. FY 2024 with start July: 2024-07-01 to 2025-06-30.
    """
    start_year = financial_year
    end_year = financial_year + 1
    from_date = date(start_year, fiscal_year_start_month, 1)
    # Last day of end month: month before next fiscal year start
    end_month = fiscal_year_start_month - 1 if fiscal_year_start_month > 1 else 12
    end_year_for_month = end_year if fiscal_year_start_month > 1 else end_year - 1
    if end_month == 12:
        to_date = date(end_year_for_month, 12, 31)
    else:
        # Last day of end_month
        from calendar import monthrange
        to_date = date(end_year_for_month, end_month, monthrange(end_year_for_month, end_month)[1])
    return from_date, to_date


def _build_pnl_api_call_plans(from_date, to_date):
    """
    Build API call plan for P&L using 31-day month anchors so period boundaries
    align to calendar month-ends (avoiding 31st-day spillover).

    Returns:
        list of (anchor_from_date, anchor_to_date, n_periods, batch_months)
        where batch_months is list of (year, month) in chronological order for that call.
    """
    desired_months = []
    cur = from_date
    while (cur.year, cur.month) <= (to_date.year, to_date.month):
        desired_months.append((cur.year, cur.month))
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    api_call_plans = []
    remaining = set(desired_months)

    while remaining:
        remaining_sorted = sorted(remaining)
        anchor_ym = None
        for ym in reversed(remaining_sorted):
            _, days = calendar.monthrange(ym[0], ym[1])
            if days == 31:
                anchor_ym = ym
                break
        if not anchor_ym:
            anchor_ym = remaining_sorted[-1]

        ay, am = anchor_ym
        _, anchor_days = calendar.monthrange(ay, am)
        anchor_from = date(ay, am, 1)
        anchor_to = date(ay, am, anchor_days)

        months_at_or_before = [ym for ym in remaining_sorted if ym <= anchor_ym]
        n_periods = min(len(months_at_or_before) - 1, 11)
        n_periods = max(n_periods, 1)

        batch = []
        y, m = ay, am
        for _ in range(n_periods + 1):
            batch.append((y, m))
            m -= 1
            if m < 1:
                m = 12
                y -= 1
        batch.reverse()

        api_call_plans.append((anchor_from, anchor_to, n_periods, batch))
        for ym in batch:
            remaining.discard(ym)

    return api_call_plans


def reconcile_reports_for_financial_year(
    tenant_id,
    financial_year,
    fiscal_year_start_month=None,
    tolerance=Decimal('0.01'),
    user=None,
):
    """
    Single process per financial year: get P&L and Balance Sheet from Xero, compare to our trail balance, return report.

    Steps:
    1. Import Profit & Loss from Xero for the financial year date range (12 months).
    2. Compare P&L to our database trail balance (per period).
    3. Import Xero Trail Balance report as at the last day of the financial year (used for balance sheet comparison).
    4. Validate balance sheet accounts (Xero TB report vs our trail balance cumulative to that date).

    Args:
        tenant_id: Xero tenant ID
        financial_year: Financial year (e.g. 2024 for FY July 2024 – June 2025 if start is July)
        fiscal_year_start_month: Month when FY starts (1–12). Default 7 (July).
        tolerance: Tolerance for numeric comparison. Default 0.01
        user: Optional user for API auth

    Returns:
        dict: {
            "financial_year": int,
            "from_date": str (YYYY-MM-DD),
            "to_date": str (YYYY-MM-DD),
            "profit_loss": {
                "import": { "report_id", "from_date", "to_date", "lines_created", "message" },
                "comparison": { "report_id", "period_stats", "overall_statistics", "message" }
            },
            "balance_sheet": {
                "import": { "report_id", "report_date", "lines_created", "message" },
                "validation": { "matches", "mismatches", "match_percentage", "details", "message" }
            },
            "success": bool,
            "errors": list of str (if any step failed)
        }
    """
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        raise ValueError(f"Tenant {tenant_id} not found")

    if fiscal_year_start_month is None:
        fiscal_year_start_month = organisation.get_fiscal_year_start_month()

    from_date, to_date = _financial_year_dates(financial_year, fiscal_year_start_month)
    today = date.today()
    last_day_current = calendar.monthrange(today.year, today.month)[1]
    # Keep all reconciliation steps aligned to the same effective end date.
    # This avoids comparing P&L to current-month data while importing BS at a future FY-end date.
    effective_to_date = min(to_date, date(today.year, today.month, last_day_current))
    from_date_str = from_date.isoformat()
    to_date_str = effective_to_date.isoformat()

    report = {
        "financial_year": financial_year,
        "from_date": from_date_str,
        "to_date": to_date_str,
        "profit_loss": {"import": None, "comparison": None},
        "balance_sheet": {"import": None, "validation": None},
        "success": True,
        "errors": [],
    }

    # —— 1. Import P&L for the financial year (up to current month) ——
    # Cap to_date to current month so we don't request future months with empty data.
    # Use a 31-day month as API anchor so period boundaries align to calendar month-ends.
    try:
        if effective_to_date < from_date:
            report["success"] = False
            report["errors"].append("P&L import: FY end is before FY start (no data yet)")
            report["profit_loss"]["import"] = {"error": "No data for this financial year yet"}
        else:
            report["to_date"] = effective_to_date.isoformat()
            to_date_str = report["to_date"]
            api_call_plans = _build_pnl_api_call_plans(from_date, effective_to_date)
            pnl_import = import_profit_loss_from_xero_reconciliation(
                tenant_id=tenant_id,
                from_date=from_date,
                to_date=effective_to_date,
                api_call_plans=api_call_plans,
                user=user,
            )
            report["profit_loss"]["import"] = {
                "report_id": pnl_import["report"].id,
                "from_date": from_date_str,
                "to_date": to_date_str,
                "lines_created": pnl_import.get("lines_created", 0),
                "message": pnl_import.get("message", "P&L imported"),
            }
    except Exception as e:
        report["success"] = False
        report["errors"].append(f"P&L import: {str(e)}")
        report["profit_loss"]["import"] = {"error": str(e)}
        logger.exception("P&L import failed for FY %s", financial_year)

    # —— 2. Compare P&L to our trail balance ——
    if report["profit_loss"]["import"] and "error" not in report["profit_loss"]["import"]:
        try:
            pnl_compare = compare_profit_loss(
                tenant_id=tenant_id,
                report_id=report["profit_loss"]["import"]["report_id"],
                tolerance=tolerance,
            )
            report["profit_loss"]["comparison"] = {
                "report_id": pnl_compare.get("report_id"),
                "period_stats": pnl_compare.get("period_stats"),
                "period_exceptions": pnl_compare.get("period_exceptions", {}),
                "overall_statistics": pnl_compare.get("overall_statistics"),
                "message": pnl_compare.get("message", "P&L comparison done"),
            }
        except Exception as e:
            report["success"] = False
            report["errors"].append(f"P&L comparison: {str(e)}")
            report["profit_loss"]["comparison"] = {"error": str(e)}
            logger.exception("P&L comparison failed for FY %s", financial_year)

    # —— 3. Import Xero Trail Balance as at effective end date (for balance sheet) ——
    try:
        tb_import = import_trail_balance_from_xero(
            tenant_id=tenant_id,
            report_date=effective_to_date,
            user=user,
        )
        report["balance_sheet"]["import"] = {
            "report_id": tb_import["report"].id,
            "report_date": to_date_str,
            "lines_created": tb_import.get("lines_created", 0),
            "message": tb_import.get("message", "Trail balance imported"),
        }
    except Exception as e:
        report["success"] = False
        report["errors"].append(f"Balance sheet (TB) import: {str(e)}")
        report["balance_sheet"]["import"] = {"error": str(e)}
        logger.exception("Trail balance import failed for FY %s (date %s)", financial_year, to_date_str)

    # —— 4. Validate balance sheet accounts (Xero TB report vs our trail balance) ——
    if report["balance_sheet"]["import"] and "error" not in report["balance_sheet"]["import"]:
        try:
            bs_validation = validate_balance_sheet_accounts(
                tenant_id=tenant_id,
                report_id=report["balance_sheet"]["import"]["report_id"],
                tolerance=tolerance,
            )
            stats = bs_validation.get("statistics") or {}
            validations = bs_validation.get("validations") or []
            report["balance_sheet"]["validation"] = {
                "matches": stats.get("matches", 0),
                "mismatches": stats.get("mismatches", 0),
                "match_percentage": stats.get("match_percentage"),
                "overall_status": bs_validation.get("overall_status"),
                "details": validations[:50],  # cap list size
                "message": bs_validation.get("message", "Balance sheet validation done"),
            }
        except Exception as e:
            report["success"] = False
            report["errors"].append(f"Balance sheet validation: {str(e)}")
            report["balance_sheet"]["validation"] = {"error": str(e)}
            logger.exception("Balance sheet validation failed for FY %s", financial_year)

    return report
