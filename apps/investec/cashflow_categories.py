"""Cash-flow category classifier for InvestecBankTransaction rows.

Rule-based (no ML, no LLM): a documented table of (transaction_type, regex/keyword)
rules that maps a bank transaction to ONE cash-flow category. This seeds the
ACTUALS side of Report 1 (the cash-flow forecast) — the forward obligations are
modelled separately in apps.investec.cashflow_forecast.

Categories (the report's in/out taxonomy):
  inflows  : rental_income, jse_dividend, anchor_income
  outflows : operating_outflow, personal_drawing, debt_service, vat
  neutral  : related_party, transfer_internal (ELIMINATE at group level —
             one economic unit, FINANCIAL_PRIORITIES.md), other

Key doctrine (FINANCIAL_PRIORITIES.md):
  - "PERESEC" credits are JSE/portfolio dividend income (jse_dividend).
  - Transfers BETWEEN the 14 internal accounts wash out at group level
    (transfer_internal) — detected when a counterparty internal account number
    appears in the description (Investec stamps "<NAME> <ACCOUNT_NUMBER>" on
    inter-account deposits/payments).
  - The Anchor note (anchor_income) pays quarterly income while held; it CEASES
    under the liquidate base case, so this category is rare in recent actuals.

Design note: deliberately small and transparent. The classifier returns a single
category string and never mutates anything. Internal-transfer detection runs
FIRST (highest precedence) because an inter-account payment must never be
double-counted as e.g. a personal drawing or operating outflow.
"""
from __future__ import annotations

import re

from apps.investec.owner_map import INTERNAL_ACCOUNT_NUMBERS

# --- Category constants -----------------------------------------------------
RENTAL_INCOME = "rental_income"
JSE_DIVIDEND = "jse_dividend"
ANCHOR_INCOME = "anchor_income"
OPERATING_OUTFLOW = "operating_outflow"
PERSONAL_DRAWING = "personal_drawing"
DEBT_SERVICE = "debt_service"
VAT = "vat"
RELATED_PARTY = "related_party"
TRANSFER_INTERNAL = "transfer_internal"
OTHER = "other"

INFLOW_CATEGORIES = frozenset({RENTAL_INCOME, JSE_DIVIDEND, ANCHOR_INCOME})
OUTFLOW_CATEGORIES = frozenset({OPERATING_OUTFLOW, PERSONAL_DRAWING, DEBT_SERVICE, VAT})
NEUTRAL_CATEGORIES = frozenset({RELATED_PARTY, TRANSFER_INTERNAL, OTHER})


# --- Keyword / regex rule tables --------------------------------------------
# Each rule is a compiled regex tested (case-insensitive) against the upper-cased
# description. Order within a table is precedence (first hit wins). The tables
# are gated by direction so a "FEES" debit -> debt_service while a deposit with
# the same token is never mis-hit.

# Internal counterparty: any of the 14 account numbers appearing in the text.
_INTERNAL_ACCOUNT_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(INTERNAL_ACCOUNT_NUMBERS)) + r")\b"
)

# CREDIT (money in) rules.
_CREDIT_RULES: list[tuple[str, re.Pattern]] = [
    (JSE_DIVIDEND,   re.compile(r"PERESEC|DIVIDEND|DIV PMT|LEGAEPERE")),
    (ANCHOR_INCOME,  re.compile(r"ANCHOR|BCI CORE|WESTBROOKE|FLEXIBLE INCOME")),
    (RENTAL_INCOME,  re.compile(r"RENT|HUUR|LEASE|TENANT|BOERDERY|HUUR|RENTAL")),
]

# DEBIT (money out) rules.
_DEBIT_RULES: list[tuple[str, re.Pattern]] = [
    (VAT,            re.compile(r"\bVAT\b|VAT201|SARS.*VAT|VALUE ADDED")),
    (DEBT_SERVICE,   re.compile(
        r"FLEXIBOND|MORTGAGE|BOND REPAYMENT|LOAN REPAYMENT|FACILITY|"
        r"INSTAL?MENT|FINANCE CHARGE|INTEREST CHARGED")),
    (RELATED_PARTY,  re.compile(
        r"TREMLY|MLD TRUST|MANAGEMENT FEE|MGMT FEE|LUCANAUDE|LUCA NAUDE")),
]

# transaction_type-level hints (apply when description rules don't match).
# These are the Investec transaction_type enum values present in the data.
_TT_DEBIT_DEFAULT = {
    "CardPurchases": OPERATING_OUTFLOW,
    "ATMWithdrawals": PERSONAL_DRAWING,
    "FeesAndInterest": DEBT_SERVICE,
    "DebitOrders": OPERATING_OUTFLOW,
    "BudgetInstalments": DEBT_SERVICE,
}


def categorise(
    description: str,
    transaction_type: str,
    direction: str,
    *,
    capacity: str | None = None,
) -> str:
    """Classify a single transaction into a cash-flow category.

    description       : InvestecBankTransaction.description
    transaction_type  : InvestecBankTransaction.transaction_type (Investec enum)
    direction         : 'CREDIT' (in) or 'DEBIT' (out) — InvestecBankTransaction.type
    capacity          : optional 'business'/'personal' (from INVESTEC_OWNER_MAP);
                        when 'personal', an unclassified DEBIT defaults to
                        personal_drawing rather than operating_outflow.

    Precedence:
      1. internal transfer (counterparty account number in description) — eliminates
      2. direction-specific keyword/regex rules
      3. transaction_type default
      4. capacity-aware fallback (personal -> personal_drawing, else operating/other)
    """
    desc = (description or "").upper()
    tt = (transaction_type or "").strip()
    is_credit = (direction or "").upper() == "CREDIT"

    # 1. Internal transfer — highest precedence (one economic unit; eliminates).
    if _INTERNAL_ACCOUNT_RE.search(desc):
        return TRANSFER_INTERNAL

    # 2. Direction-specific keyword rules.
    rules = _CREDIT_RULES if is_credit else _DEBIT_RULES
    for category, pattern in rules:
        if pattern.search(desc):
            return category

    # 3. transaction_type default (debits only — credits without a keyword are
    #    genuinely 'other' income we don't recognise).
    if not is_credit:
        tt_cat = _TT_DEBIT_DEFAULT.get(tt)
        if tt_cat is not None:
            return tt_cat

    # 4. Capacity-aware fallback.
    if not is_credit:
        if (capacity or "") == "personal":
            return PERSONAL_DRAWING
        return OPERATING_OUTFLOW
    return OTHER


def is_inflow(category: str) -> bool:
    return category in INFLOW_CATEGORIES


def is_outflow(category: str) -> bool:
    return category in OUTFLOW_CATEGORIES


def eliminates_at_group(category: str) -> bool:
    """transfer_internal washes out at group level (and related_party is internal too)."""
    return category in (TRANSFER_INTERNAL, RELATED_PARTY)
