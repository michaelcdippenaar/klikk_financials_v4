# TM1 Cube Catalog (RAG Seed)

## Core Cubes by Layer

| Cube | Layer | Purpose | Typical Use |
|---|---|---|---|
| `gl_src_trial_balance` | `src` | Imported Xero GL trial balance (read-only source layer) | Actuals lookup and source verification |
| `gl_pln_forecast` | `pln` | GL forecast planning layer | Budget/forecast input and scenario modeling |
| `gl_rpt_trial_balance` | `rpt` | Reporting-focused trial balance output | Management reporting and month/FY analysis |
| `cashflow_cnt_mapping` | `cnt` | Account-to-cashflow routing config | Cashflow classification logic |
| `cashflow_cal_metrics` | `cal` | Calculated cashflow metrics | Intermediate cashflow computations |
| `cashflow_rpt_summary` | `rpt` | Cashflow reporting summary | Cashflow statements and trend views |
| `listed_share_src_holdings` | `src` | Imported share holdings snapshots | Portfolio position analysis |
| `listed_share_src_transactions` | `src` | Imported share transaction activity | Buy/sell/dividend activity review |
| `listed_share_cal_flow_metrics` | `cal` | Calculated share performance metrics | Yield/TWRR-like analytical outputs |
| `listed_share_pln_forecast` | `pln` | Dividend forecast planning cube | Budgeted dividend projections and overrides |
| `prop_res_pln_forecast_revenue` | `pln` | Property revenue forecast planning | Property planning workflows |
| `sys_parameters` | `sys` | Current period and model control parameters | Runtime defaults and period context |

## Important Modeling Notes

- Source cubes (`src`) are treated as ingestion outputs and should generally be read-only in normal workflows.
- Planning cubes (`pln`) are the intended write targets for user adjustments and scenario work.
- Reporting cubes (`rpt`) are presentation-oriented structures for stable analysis.
- Configuration cubes (`cnt`) drive mapping/routing behavior and should be changed carefully.

## Special Focus: Dividend Planning

The `listed_share_pln_forecast` cube is central for dividend budget workflows:

- Stores baseline calculated values and explicit declared-dividend adjustments
- Supports dry-run then confirm-write workflow for safety
- Enables budget versus declared comparisons over planning periods

## Special Focus: System Parameters

`sys_parameters` is used to store model-wide controls such as:

- Current month
- Current year
- Financial year context

These parameters are frequently used by tools and prompts to interpret "current period" requests.
