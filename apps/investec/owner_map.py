"""INVESTEC_OWNER_MAP — entity / capacity attribution for the 14 Investec accounts.

The 14 Investec Private Bank accounts span MC's whole economic unit (Klikk, the
trusts, LucaNaude, and the personal/household accounts). The forecast and the
cash-actuals view need to roll transactions up to ENTITY and CAPACITY so the
report can show business-vs-personal cash combined (FINANCIAL_PRIORITIES.md
priority #1 — "manage cash in both business AND personal capacity").

Attribution is unambiguous from account_number (surveyed against the live DB,
2026-06-25). Keyed by account_number → {entity, capacity, liquid}:

  entity     ∈ {Klikk, MC personal, MLD Trust, Testamentary Trust,
                Lucanaude, L Dippenaar}
  capacity   ∈ {business, personal}
  liquid     bool — True for transactional / cash accounts that count toward the
             group LIQUID opening cash position; False for the Mortgage Loan
             Accounts (363177001 / 363177003), which are DEBT facilities, not
             spendable cash. Summing them into "opening cash" would understate
             the liquid position by the drawn loan balance.

The Mortgage Loan Accounts carry account_name "Klikk (Pty) Ltd" in the API but
are the "Klikk Mortgage Loan Account" per the entity survey — they are Klikk
business *debt*, hence liquid=False.

This module is pure config + a small idempotent backfill helper. No I/O at
import time.
"""
from __future__ import annotations

# Entity constants — used by the view, the forecast engine, and the accessor.
KLIKK = "Klikk"
MC_PERSONAL = "MC personal"
MLD_TRUST = "MLD Trust"
TESTAMENTARY_TRUST = "Testamentary Trust"
LUCANAUDE = "Lucanaude"
L_DIPPENAAR = "L Dippenaar"

BUSINESS = "business"
PERSONAL = "personal"


# account_number → attribution. The single source of truth.
INVESTEC_OWNER_MAP: dict[str, dict] = {
    # --- MC personal (household / personal capacity) ---
    "10011910139":   {"entity": MC_PERSONAL,        "capacity": PERSONAL, "liquid": True},
    "1100588588501": {"entity": MC_PERSONAL,        "capacity": PERSONAL, "liquid": True},
    "1100588588540": {"entity": MC_PERSONAL,        "capacity": PERSONAL, "liquid": True},
    # --- L Dippenaar (personal) ---
    "10014792677":   {"entity": L_DIPPENAAR,        "capacity": PERSONAL, "liquid": True},
    "1100116634500": {"entity": L_DIPPENAAR,        "capacity": PERSONAL, "liquid": True},
    # --- Klikk (Pty) Ltd — business operating cash ---
    "10011924075":   {"entity": KLIKK,              "capacity": BUSINESS, "liquid": True},
    # --- Klikk Mortgage Loan Accounts — business DEBT (not liquid cash) ---
    "363177001":     {"entity": KLIKK,              "capacity": BUSINESS, "liquid": False},
    "363177003":     {"entity": KLIKK,              "capacity": BUSINESS, "liquid": False},
    # --- MLD Trust ---
    "10013017883":   {"entity": MLD_TRUST,          "capacity": BUSINESS, "liquid": True},
    "1100591058540": {"entity": MLD_TRUST,          "capacity": BUSINESS, "liquid": True},
    # --- MC Dippenaar Testamentary Trust ---
    "10013648134":   {"entity": TESTAMENTARY_TRUST, "capacity": BUSINESS, "liquid": True},
    "1100603381540": {"entity": TESTAMENTARY_TRUST, "capacity": BUSINESS, "liquid": True},
    # --- Lucanaude (Pty) Ltd ---
    "10014572054":   {"entity": LUCANAUDE,          "capacity": BUSINESS, "liquid": True},
    "1100612949540": {"entity": LUCANAUDE,          "capacity": BUSINESS, "liquid": True},
}

# The set of all 14 internal account numbers. Used by the cash-flow category map
# to detect internal transfers (counterparty account number appears in the
# transaction description) — these ELIMINATE at group level (one economic unit).
INTERNAL_ACCOUNT_NUMBERS: frozenset[str] = frozenset(INVESTEC_OWNER_MAP.keys())

# Liquid account numbers — those that count toward group opening cash.
LIQUID_ACCOUNT_NUMBERS: frozenset[str] = frozenset(
    acc for acc, attr in INVESTEC_OWNER_MAP.items() if attr["liquid"]
)


def attribution_for(account_number: str) -> dict | None:
    """Return the {entity, capacity, liquid} attribution for an account number, or None."""
    return INVESTEC_OWNER_MAP.get((account_number or "").strip())


def owner_label(account_number: str) -> str:
    """The string written to InvestecBankAccount.owner — the entity name.

    Empty string for an unmapped account (defensive — every live account is
    mapped, but a future account would surface as '' rather than crash).
    """
    attr = attribution_for(account_number)
    return attr["entity"] if attr else ""


def backfill_owners(*, dry_run: bool = False) -> dict:
    """Idempotently set InvestecBankAccount.owner from INVESTEC_OWNER_MAP.

    Only writes when the stored owner differs from the mapped entity, so re-runs
    are no-ops. Never deletes; never touches transactions. Returns a stats dict.

    dry_run=True computes the changes without writing.
    """
    # Imported here so the module stays import-safe outside a Django context.
    from apps.investec.models import InvestecBankAccount

    stats = {"updated": 0, "unchanged": 0, "unmapped": 0, "dry_run": dry_run, "changes": []}
    for acc in InvestecBankAccount.objects.all().order_by("account_number"):
        target = owner_label(acc.account_number)
        if not target:
            stats["unmapped"] += 1
            stats["changes"].append({"account_number": acc.account_number, "action": "UNMAPPED"})
            continue
        if acc.owner == target:
            stats["unchanged"] += 1
            continue
        stats["changes"].append(
            {"account_number": acc.account_number, "from": acc.owner, "to": target}
        )
        if not dry_run:
            acc.owner = target
            acc.save(update_fields=["owner", "updated_at"])
        stats["updated"] += 1
    return stats
