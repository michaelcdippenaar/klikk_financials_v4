"""
Utility functions for fiscal year and financial period calculations.
"""

# Default when Xero organisation settings are unavailable
DEFAULT_FISCAL_YEAR_START_MONTH = 7


def financial_year_end_to_start_month(financial_year_end_month):
    """
    Convert Xero's financial_year_end_month to fiscal_year_start_month.
    E.g. FY ends June (6) → FY starts July (7); FY ends Dec (12) → FY starts Jan (1).
    """
    if financial_year_end_month is None:
        return DEFAULT_FISCAL_YEAR_START_MONTH
    return (financial_year_end_month % 12) + 1


def fiscal_year_to_financial_year(year, month, fiscal_year_start_month):
    """
    Convert calendar year/month to financial year.
    
    Args:
        year: Calendar year
        month: Calendar month (1-12)
        fiscal_year_start_month: Month when fiscal year starts (1-12)
    
    Returns:
        Financial year (integer)
    """
    if month >= fiscal_year_start_month:
        return year
    else:
        return year - 1


def fiscal_month_to_financial_period(month, fiscal_year_start_month):
    """
    Convert calendar month to financial period.
    
    Args:
        month: Calendar month (1-12)
        fiscal_year_start_month: Month when fiscal year starts (1-12)
    
    Returns:
        Financial period (1-12)
    """
    if month >= fiscal_year_start_month:
        return month - fiscal_year_start_month + 1
    else:
        return month + (12 - fiscal_year_start_month) + 1

