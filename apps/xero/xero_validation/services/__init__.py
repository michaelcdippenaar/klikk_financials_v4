"""
Xero Validation Services Package

This package contains modular services for validation functionality.
All public functions are re-exported here for backward compatibility.
"""

# Re-export all public functions for backward compatibility
from .helpers import convert_decimals_to_strings
# Also available from helpers.service_helpers
from ..helpers.service_helpers import convert_decimals_to_strings as convert_decimals_to_strings_helper
from .imports import (
    import_trial_balance_from_file,
    import_trail_balance_from_xero,
    import_profit_loss_from_xero,
    import_and_export_trail_balance,
)
from .comparisons import (
    compare_trail_balance,
    compare_profit_loss,
)
from .exports import (
    export_all_line_items_to_csv,
    export_trail_balance_report_complete,
    export_profit_loss_report_complete,
    export_report_to_files,
)
from .validation import (
    validate_balance_sheet_complete,
    validate_balance_sheet_accounts,
)
from .reconciliation import reconcile_reports_for_financial_year
from .income_statement import (
    add_income_statement_to_trail_balance_report,
)

__all__ = [
    # Helpers
    'convert_decimals_to_strings',
    # Imports
    'import_trial_balance_from_file',
    'import_trail_balance_from_xero',
    'import_profit_loss_from_xero',
    'import_and_export_trail_balance',
    # Comparisons
    'compare_trail_balance',
    'compare_profit_loss',
    # Exports
    'export_all_line_items_to_csv',
    'export_trail_balance_report_complete',
    'export_profit_loss_report_complete',
    'export_report_to_files',
    # Validation
    'validate_balance_sheet_complete',
    'validate_balance_sheet_accounts',
    'reconcile_reports_for_financial_year',
    # Income Statement
    'add_income_statement_to_trail_balance_report',
]

