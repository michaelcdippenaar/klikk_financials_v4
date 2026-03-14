# TM1 Model Overview (Klikk Group Planning V3)

## Purpose

This TM1 model is the core planning and reporting model for Klikk Group financials.
It combines imported operational data (Xero, Investec, and other sources) with calculated logic and planning inputs so users can:

- Analyze actual performance
- Build budgets and forecasts
- Run dividend and cashflow planning
- Produce management-ready reports

## Platform Context

- Engine: IBM Planning Analytics (TM1)
- Default server target in configuration: `192.168.1.194:44414`
- Configuration source: Django settings (`TM1_CONFIG`)

## Module Scope

The model is organized into business modules including:

- `gl` (general ledger / Xero)
- `listed_share` (portfolio and dividend planning)
- `cashflow` (cashflow routing and summaries)
- `hierarchy` (shared dimensions and structures)
- `sys` (global parameters)
- Additional configured modules include: `prop_res`, `prop_agr`, `financing`, `equip_rental`, `cost_alloc`

## Layered Data Architecture

The model follows a consistent layer pipeline:

1. `src` - imported source data (system of record snapshots)
2. `cal` - calculated logic and transformations
3. `pln` - planning and forecast assumptions
4. `rpt` - reporting-ready outputs

Supporting layers:

- `cnt` - configuration and mappings
- `sys` - global control parameters

## Naming Convention

Primary naming pattern:

- Cubes: `<module>_<layer>_<description>`
- Processes: `<scope>.<object>.<action>`

Examples:

- `gl_src_trial_balance`
- `cashflow_cal_metrics`
- `listed_share_pln_forecast`
- `cub.gl_src_trial_balance.import`

## Planning Philosophy

The model separates:

- **Imported actuals** (trusted source snapshots)
- **Derived calculations** (rule/process driven)
- **User planning adjustments** (explicitly controlled writes)
- **Report outputs** (stable structures for consumption)

This separation makes it easier to audit where a number came from and whether it is source, calculated, or planned.
