# TM1 Workflows and Guardrails (RAG Seed)

## Main Workflows Supported

## 1) GL Planning and Reporting

- Ingest trial balance actuals into source structures (`gl_src_*`)
- Plan and adjust forecast values in planning structures (`gl_pln_*`)
- Consume standardized outputs in reporting structures (`gl_rpt_*`)

## 2) Cashflow Planning

- Maintain mapping logic in configuration (`cashflow_cnt_mapping`)
- Use calculated cashflow metrics (`cashflow_cal_metrics`)
- Report via summary structures (`cashflow_rpt_summary`)

## 3) Listed Share and Dividend Planning

- Use holdings and transactions source cubes for portfolio baseline
- Calculate performance/flow metrics in `listed_share_cal_flow_metrics`
- Apply planned dividend assumptions and declared adjustments in `listed_share_pln_forecast`

## 4) Current Period / Runtime Context

- Use `sys_parameters` to resolve current month/year/financial-year context
- Apply this context before running period-sensitive reports

## Safety and Operational Guardrails

## Write Control

- Planning writes should use explicit confirmation patterns (dry-run first, confirm second).
- Source (`src`) and reporting (`rpt`) layers are generally not write targets for normal user workflows.

## Process Execution

- Process-style actions should support pre-check and confirmation before execution.
- Prefer transparent user confirmation for actions that can change model state.

## Data Interpretation

- Do not assume element names are user-friendly; use dimension attributes/aliases.
- Validate element matches before writing or running critical workflows.

## Assumptions to Track in RAG

- Default TM1 server is configured in `TM1_CONFIG` and may be environment-overridden.
- Financial period interpretation depends on system parameter settings.
- Some workflows depend on external data freshness (imports from Xero/Investec and related stores).

## Suggested Metadata Tags for Ingestion

Use these tags when ingesting this file family:

- `tm1`
- `planning`
- `model-architecture`
- `cube-reference`
- `dimension-reference`
- `workflow-guardrails`
