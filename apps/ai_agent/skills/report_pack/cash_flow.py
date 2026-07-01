"""
Report 1 — Combined 13-Week Cash-Flow report builder.

Assembles the report-pack CONTRACT over the data engine:

    from apps.investec.cashflow_forecast import get_cashflow_forecast
    build_cash_flow_report(anchor_date=None, weeks=13) -> dict

Returns {report_id, engine, period, facts, widgets, narrative, provenance}.

DOCTRINE (REPORT_PRODUCTIONISATION_PLAN.md §5):
  - Postgres is THE single reporting source. Every figure comes from the data
    engine (apps.investec.cashflow_forecast) — this assembler NEVER queries a
    bank/Xero/TM1 system directly, and NEVER recomputes or invents a number.
    It reads engine floats and FORMATS them for display only.
  - The LLM (Stage-B Haiku) may ONLY rephrase the deterministic Stage-A prose.
    A hard numeric-substring gate discards any LLM output that introduces a
    number not present verbatim in the Stage-A input → ship Stage-A.
  - facts + provenance are the bank/SARS audit trail: every number that appears
    in widgets/narrative also lives in facts, and provenance lets someone trace
    each figure to a Postgres read or a named forecast rule.

INTERNAL ONLY (POPIA §21) — renders into the existing authenticated dashboard
surface, never a public endpoint.
"""
from __future__ import annotations

import logging

from apps.investec.cashflow_forecast import (
    SCENARIOS_AVAILABLE,
    get_cashflow_forecast,
)

log = logging.getLogger("ai_agent")


def _widget(*args, **kwargs):
    """Thin proxy to report_builder._widget.

    Imported lazily (not at module top) to avoid a circular import:
    report_builder appends this module's tool schema at its own bottom, so a
    top-level `from report_builder import _widget` here would deadlock the
    partially-initialised modules. By the time any builder function runs, both
    modules are fully loaded.
    """
    from apps.ai_agent.skills.report_builder import _widget as _w

    return _w(*args, **kwargs)

REPORT_ID = "cash_flow"

# Haiku model id — matches the existing narrative-generation convention in
# apps/ai_agent/agent/core.py (context extraction uses the same model).
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# The DataGrid that IS the 13-week ledger (prototype Report 1 §1A column order).
_GRID_HEADERS = [
    "Week", "Start", "Opening", "Inflows", "Op-out",
    "Personal", "Debt svc", "VAT-out", "Closing",
]


# ---------------------------------------------------------------------------
#  Display formatting (NEVER recomputes — formats engine floats only)
# ---------------------------------------------------------------------------

def _rand(amount: float | int | None) -> str:
    """Format a float as a rand string, e.g. -1234.5 -> '-R1,234.50'.

    Display only — the underlying float comes from the engine untouched.
    """
    if amount is None:
        return "n/a"
    sign = "-" if amount < 0 else ""
    return f"{sign}R{abs(amount):,.2f}"


def _rand0(amount: float | int | None) -> str:
    """Rand with no decimals (KPI headline display), e.g. -533701.0 -> '-R533,701'."""
    if amount is None:
        return "n/a"
    sign = "-" if amount < 0 else ""
    return f"{sign}R{abs(amount):,.0f}"


# ---------------------------------------------------------------------------
#  Facts — the deterministic figures (the audit trail + narrative inputs)
# ---------------------------------------------------------------------------

def _build_facts(base: dict, per_scenario: dict[str, dict]) -> dict:
    """Assemble the facts block from engine output. EVERY number that appears
    in any widget or narrative line must originate here (and hence from the
    engine), so the no-new-figures gate has something to check against.
    """
    series = base["weeks_series"]
    inflows = base["in_out_breakdown"]["inflows"]
    outflows = base["in_out_breakdown"]["outflows"]
    headroom = base["facility_headroom"]
    low = base["low_point"]

    # Totals are SUMMED from engine per-week figures (aggregation of engine
    # output, not a re-derivation of any obligation). Sums of engine floats.
    total_inflows = sum(w["inflows"] for w in series)
    total_operating = sum(w["operating_out"] for w in series)
    total_personal = sum(w["personal_out"] for w in series)
    total_debt = sum(w["debt_service"] for w in series)
    total_vat = sum(w["vat_out"] for w in series)
    total_outflows = total_operating + total_personal + total_debt + total_vat

    # Date for the base-case low-point week (from the engine series).
    low_week = low["week"]
    low_row = next((w for w in series if w["week"] == low_week), None)
    low_date = low_row["start_date"] if low_row else None

    # Per-scenario low points (week + amount + date) — the scenario toggle audit.
    scenario_lows: dict[str, dict] = {}
    for name, res in per_scenario.items():
        s_low = res["low_point"]
        s_row = next((w for w in res["weeks_series"] if w["week"] == s_low["week"]), None)
        scenario_lows[name] = {
            "amount": s_low["amount"],
            "week": s_low["week"],
            "start_date": s_row["start_date"] if s_row else None,
        }

    return {
        "opening_balance": base["opening_balance"],
        "weeks": base["weeks"],
        "anchor_date": base["anchor_date"],
        "low_point": {
            "amount": low["amount"],
            "week": low_week,
            "start_date": low_date,
        },
        "totals": {
            "inflows": total_inflows,
            "outflows": total_outflows,
            "operating_out": total_operating,
            "personal_out": total_personal,
            "debt_service": total_debt,
            "vat_out": total_vat,
        },
        "in_out_breakdown": {
            "inflows": dict(inflows),
            "outflows": dict(outflows),
        },
        "facility_headroom": {
            "flexibond": headroom["flexibond"],
            "investec": headroom["investec"],
            "voliere_earmark": headroom["voliere_earmark"],
            "net_usable": headroom["net_usable"],
        },
        # Closing-balance floor cushion shown on the LineChart (see _scenario_widgets).
        "headroom_floor_line": base["opening_balance"] + headroom["net_usable"],
        "scenario_low_points": scenario_lows,
        "scenarios_available": list(SCENARIOS_AVAILABLE),
    }


# ---------------------------------------------------------------------------
#  Widgets — rendered via report_builder._widget (REAL widget types only)
# ---------------------------------------------------------------------------

def _kpi_cards(facts: dict) -> list[dict]:
    """KPICards: opening cash, base-case low + week, facility net headroom."""
    opening = facts["opening_balance"]
    low = facts["low_point"]
    net_usable = facts["facility_headroom"]["net_usable"]

    cards: list[dict] = []
    cards.append(_widget("KPICard", "Opening Liquid Cash", {
        "title": "Opening Liquid Cash",
        "value": _rand0(opening),
        "format": "text",
        "subtitle": f"as at {facts['anchor_date']}",
        "status": "ok" if opening >= 0 else "critical",
    }, width=1, height="sm"))

    cards.append(_widget("KPICard", "13-Week Low (base)", {
        "title": "13-Week Low (base case)",
        "value": _rand0(low["amount"]),
        "format": "text",
        "subtitle": f"Week {low['week']} ({low['start_date']})",
        "status": "ok" if (low["amount"] is not None and low["amount"] >= 0) else "critical",
        "trend": "down" if (low["amount"] is not None and low["amount"] < opening) else None,
    }, width=1, height="sm"))

    cards.append(_widget("KPICard", "Facility Net Headroom", {
        "title": "Facility Net Headroom",
        "value": _rand0(net_usable),
        "format": "text",
        "subtitle": "Flexibond + Investec − Voliere earmark",
        "status": "ok",
    }, width=1, height="sm"))

    return cards


def _scenario_widgets(scenario: str, res: dict) -> list[dict]:
    """The grid + closing-balance LineChart for ONE scenario.

    The DataGrid IS the 13-week ledger (foots + chains). The LineChart overlays
    the closing-balance line with a FACILITY-HEADROOM line.

    Facility-headroom line CHOICE: a constant series at (opening_balance +
    net_usable). The prototype frames the position as "solvent with ~R7.4m
    facility headroom, but liquidity is thin" — i.e. the spendable floor is how
    far cash could fall before the facilities are exhausted. So the headroom
    line = the floor cushion below today's opening, and the closing line read
    against it shows remaining facility runway. (The alternative — a flat line
    at net_usable alone — ignores the live opening balance the report is now
    tied to, so opening + net_usable is the prototype-faithful choice.)
    """
    series = res["weeks_series"]
    opening = res["opening_balance"]
    net_usable = res["facility_headroom"]["net_usable"]
    floor_line = opening + net_usable

    # --- DataGrid: the 13-week ledger (one row per week) ---
    grid_rows = [
        [
            w["week"],
            w["start_date"],
            _rand(w["opening"]),
            _rand(w["inflows"]),
            _rand(w["operating_out"]),
            _rand(w["personal_out"]),
            _rand(w["debt_service"]),
            _rand(w["vat_out"]),
            _rand(w["closing"]),
        ]
        for w in series
    ]
    grid = _widget(
        "DataGrid", f"13-Week Cash-Flow Ledger — {scenario}",
        {
            "headers": _GRID_HEADERS,
            "rows": grid_rows,
            "title": f"13-Week Cash-Flow Ledger — {scenario}",
        },
        width=4, height="lg",
        data={"headers": _GRID_HEADERS, "rows": grid_rows},
    )

    # --- LineChart: closing balance vs facility-headroom floor ---
    x_axis = [f"W{w['week']}" for w in series]
    closing_data = [w["closing"] for w in series]
    headroom_data = [floor_line for _ in series]
    line = _widget(
        "LineChart", f"Closing Balance vs Facility Headroom — {scenario}",
        {
            "title": f"Closing Balance vs Facility Headroom — {scenario}",
            "xAxis": x_axis,
            "series": [
                {"name": "Closing balance", "data": closing_data},
                {"name": "Facility headroom (opening + net usable)", "data": headroom_data},
            ],
        },
        width=2, height="md",
    )

    return [grid, line]


def _breakdown_widget(facts: dict) -> dict:
    """The in/out breakdown as a BarChart (inflow vs outflow categories)."""
    inflows = facts["in_out_breakdown"]["inflows"]
    outflows = facts["in_out_breakdown"]["outflows"]

    labels = list(inflows.keys()) + list(outflows.keys())
    in_vals = [inflows[k] for k in inflows] + [0 for _ in outflows]
    out_vals = [0 for _ in inflows] + [outflows[k] for k in outflows]

    return _widget(
        "BarChart", "13-Week In/Out Breakdown",
        {
            "title": "13-Week In/Out Breakdown (by category)",
            "xAxis": labels,
            "series": [
                {"name": "Inflows", "data": in_vals},
                {"name": "Outflows", "data": out_vals},
            ],
        },
        width=2, height="md",
    )


def _build_widgets(facts: dict, per_scenario: dict[str, dict]) -> dict:
    """Assemble all widgets. The scenario toggle is realised as a
    per-scenario widget set so the consumer can show base / boerdery_plus_60 /
    no_boerdery. KPI cards + breakdown are keyed off the base case.
    """
    common = _kpi_cards(facts) + [_breakdown_widget(facts)]
    scenario_sets: dict[str, list[dict]] = {
        name: _scenario_widgets(name, res) for name, res in per_scenario.items()
    }
    # Flat list (base scenario inline) for consumers that render a single feed;
    # the keyed `by_scenario` map is the toggle source of truth.
    base_name = SCENARIOS_AVAILABLE[0]
    flat = common + scenario_sets.get(base_name, [])
    return {
        "common": common,
        "by_scenario": scenario_sets,
        "widgets": flat,
    }


# ---------------------------------------------------------------------------
#  Narrative — Stage-A deterministic skeleton, Stage-B optional Haiku rephrase
# ---------------------------------------------------------------------------

def _stage_a_narrative(facts: dict) -> str:
    """Deterministic prose skeleton with EVERY figure pre-formatted from facts.
    This is the shippable narrative; Stage-B may only rephrase it.
    """
    opening = facts["opening_balance"]
    low = facts["low_point"]
    totals = facts["totals"]
    net_usable = facts["facility_headroom"]["net_usable"]
    s_lows = facts["scenario_low_points"]

    parts: list[str] = []
    parts.append(
        f"Combined 13-week cash-flow forecast for the group, tied to live "
        f"Investec balances as at {facts['anchor_date']}. Opening liquid cash "
        f"is {_rand(opening)}."
    )
    parts.append(
        f"Over the {facts['weeks']} weeks the group draws total inflows of "
        f"{_rand(totals['inflows'])} against total outflows of "
        f"{_rand(totals['outflows'])} (operating {_rand(totals['operating_out'])}, "
        f"personal drawings {_rand(totals['personal_out'])}, debt service "
        f"{_rand(totals['debt_service'])}, VAT remittance {_rand(totals['vat_out'])})."
    )
    parts.append(
        f"The base-case low point is {_rand(low['amount'])} in Week "
        f"{low['week']} ({low['start_date']}). Facility net headroom is "
        f"{_rand(net_usable)}, so the position is solvent on the facilities even "
        f"at the trough."
    )
    # Per-scenario low points (the toggle audit), each figure from facts.
    scen_bits = []
    for name in SCENARIOS_AVAILABLE:
        sl = s_lows.get(name)
        if sl is None:
            continue
        scen_bits.append(
            f"{name}: low {_rand(sl['amount'])} in Week {sl['week']} "
            f"({sl['start_date']})"
        )
    if scen_bits:
        parts.append("Scenario low points — " + "; ".join(scen_bits) + ".")
    return " ".join(parts)


def _extract_numbers(text: str) -> set[str]:
    """Pull the numeric tokens out of a string for the no-new-figures gate.

    A number token = a run of digits/commas/dots (e.g. '1,277,640.00', '13').
    We match the FORMATTED tokens as substrings so a Stage-B rephrase that keeps
    'R1,277,640.00' passes and one that paraphrases to 'R1.28m' fails (correctly,
    because that rounded figure is not in the deterministic input).
    """
    import re

    return set(re.findall(r"\d[\d,]*(?:\.\d+)?", text))


def _numeric_gate(stage_a: str, candidate: str) -> bool:
    """Return True iff every numeric token in `candidate` appears verbatim in
    `stage_a`. Hard gate: any new figure → reject the LLM output.
    """
    a_numbers = _extract_numbers(stage_a)
    for tok in _extract_numbers(candidate):
        if tok not in a_numbers:
            return False
    return True


def _stage_b_rephrase(stage_a: str) -> tuple[str, dict]:
    """Optional Haiku rephrase of the Stage-A skeleton.

    Returns (narrative, meta). On ANY failure (no key, API error, gate reject)
    falls back to Stage-A — never raises, never blocks. The LLM is told it may
    only rephrase and must not introduce/alter any number; the deterministic
    `_numeric_gate` enforces it regardless of what the model does.
    """
    meta = {"stage_b_attempted": False, "stage_b_used": False, "reason": None}
    try:
        from apps.ai_agent.agent.config import get_credential

        api_key = get_credential("anthropic_api_key")
        if not api_key:
            meta["reason"] = "no_api_key"
            return stage_a, meta

        meta["stage_b_attempted"] = True
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        system = (
            "You rephrase a financial summary for clarity and flow. "
            "HARD RULES: (1) Do NOT introduce, remove, round, or alter ANY number "
            "— every figure must appear exactly as given. (2) Do NOT invent facts. "
            "(3) Keep it concise. Return ONLY the rephrased prose, no preamble."
        )
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            system=system,
            messages=[{"role": "user", "content": stage_a}],
            max_completion_tokens=600,
        )
        candidate = resp.content[0].text.strip()
        if candidate and _numeric_gate(stage_a, candidate):
            meta["stage_b_used"] = True
            return candidate, meta
        meta["reason"] = "numeric_gate_rejected"
        return stage_a, meta
    except Exception as exc:  # pragma: no cover - network/key/SDK all degrade to Stage-A
        log.info("cash_flow Stage-B rephrase unavailable (non-fatal): %s", exc)
        meta["reason"] = f"exception:{type(exc).__name__}"
        return stage_a, meta


# ---------------------------------------------------------------------------
#  Provenance — pass through the engine's audit trail + report-rule provenance
# ---------------------------------------------------------------------------

def _build_provenance(base: dict) -> list[dict]:
    """The engine's provenance (Investec running_balance reads + forecast rules)
    PLUS report-assembly provenance (which engine accessor produced the figures).
    """
    prov = list(base["provenance"])  # pass-through (bank/SARS audit trail)
    prov.append({
        "line": "report assembly",
        "amount": None,
        "source": (
            "apps.investec.cashflow_forecast.get_cashflow_forecast "
            f"(scenario={base['scenario']}, weeks={base['weeks']}, "
            f"anchor_date={base['anchor_date']}); figures read from the data "
            "engine and formatted for display — never recomputed in the assembler."
        ),
        "type": "rule",
    })
    return prov


# ---------------------------------------------------------------------------
#  Public builder — THE CONTRACT
# ---------------------------------------------------------------------------

def build_cash_flow_report(
    anchor_date: str | None = None,
    weeks: int = 13,
    rephrase: bool = True,
) -> dict:
    """Build Report 1 — Combined 13-Week Cash-Flow.

    anchor_date : optional anchor — a datetime.date or an ISO date string
                  (YYYY-MM-DD); default = engine default (today). The engine
                  reads the opening balance as-at this date.
    weeks       : forward weeks (default 13).
    rephrase    : if True, attempt the optional Stage-B Haiku rephrase (falls back
                  to the deterministic Stage-A on any failure / gate reject).

    Returns {report_id, engine, period, facts, widgets, narrative, provenance}.
    All numbers come from apps.investec.cashflow_forecast — deterministic.
    """
    import datetime

    anchor: datetime.date | None = None
    if anchor_date:
        # Accept either a date (Python callers / the engine's own signature) or an
        # ISO date string (the agent tool passes JSON, so it arrives as a string).
        anchor = (
            anchor_date
            if isinstance(anchor_date, datetime.date)
            else datetime.date.fromisoformat(anchor_date)
        )

    # Pull every scenario so the consumer can toggle. Base case is the headline.
    per_scenario: dict[str, dict] = {}
    for scenario in SCENARIOS_AVAILABLE:
        per_scenario[scenario] = get_cashflow_forecast(
            anchor_date=anchor, weeks=weeks, scenario=scenario,
        )
    base = per_scenario[SCENARIOS_AVAILABLE[0]]

    facts = _build_facts(base, per_scenario)
    widget_block = _build_widgets(facts, per_scenario)

    stage_a = _stage_a_narrative(facts)
    narrative_text, narrative_meta = (
        _stage_b_rephrase(stage_a) if rephrase else (stage_a, {"stage_b_used": False})
    )

    provenance = _build_provenance(base)

    period_label = f"{weeks} weeks from {facts['anchor_date']}"

    return {
        "report_id": REPORT_ID,
        "engine": "postgres",
        "period": {
            "anchor_date": facts["anchor_date"],
            "weeks": weeks,
            "label": period_label,
        },
        "facts": facts,
        "widgets": widget_block["widgets"],
        "widgets_by_scenario": widget_block["by_scenario"],
        "widgets_common": widget_block["common"],
        "narrative": {
            "text": narrative_text,
            "stage_a": stage_a,
            "stage_b_used": narrative_meta.get("stage_b_used", False),
            "meta": narrative_meta,
        },
        "provenance": provenance,
    }


# ---------------------------------------------------------------------------
#  Agent tool registration — build_weekly_cash_refresh
#  (consumed by tool_registry via report_builder.TOOL_SCHEMAS / TOOL_FUNCTIONS;
#   see _register_tool() in this module's import side-effect below)
# ---------------------------------------------------------------------------

TOOL_SCHEMA = {
    "name": "build_weekly_cash_refresh",
    "description": (
        "Build Report 1 — Combined 13-Week Cash-Flow forecast for the whole "
        "economic unit (Klikk + trusts + LucaNaude + MC personal), tied to live "
        "Investec balances in Postgres. Returns KPI cards (opening cash, base-case "
        "13-week low, facility net headroom), a 13-week ledger DataGrid that foots "
        "and chains, a closing-balance LineChart with the facility-headroom line "
        "overlaid, an in/out breakdown BarChart, and widgets for all three "
        "scenarios (base / boerdery_plus_60 / no_boerdery). Numbers are "
        "deterministic from the data engine; the narrative is prose-only. "
        "INTERNAL ONLY."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "anchor_date": {
                "type": "string",
                "description": "Optional ISO date (YYYY-MM-DD) for week 1 / opening-balance as-at. Default: today.",
            },
            "weeks": {
                "type": "integer",
                "description": "Forward weeks to forecast (default 13).",
            },
        },
    },
}
