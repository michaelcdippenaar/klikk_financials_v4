"""
Report Pack — productionised group financial reports (v2 prototype → live data).

Thin assembler layer over the per-report builder functions. Each builder returns
the report-pack CONTRACT:

    {report_id, engine, period, facts, widgets, narrative, provenance}

Deterministic numbers (every figure comes from the data engine, never invented or
recomputed in the assembler), LLM prose-only for the narrative with a hard
no-new-figures gate, and widgets rendered via the existing report_builder._widget
helper so they display in the existing authenticated dashboard surface.

TM1 / cash data is INTERNAL ONLY (POPIA §21) — builders return data for the existing
authenticated dashboard surface, never a public endpoint.

Reports:
- cash_flow: Report 1 — Combined 13-Week Cash-Flow (PG/Bank engine).

Agent tool entry: build_weekly_cash_refresh (wraps cash_flow.build_cash_flow_report).
"""
from __future__ import annotations

from apps.ai_agent.skills.report_pack import cash_flow

__all__ = ["cash_flow"]
