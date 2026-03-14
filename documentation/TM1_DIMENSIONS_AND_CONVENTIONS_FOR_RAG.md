# TM1 Dimensions and Conventions (RAG Seed)

## Key Shared Dimensions

| Dimension | Description | Notes |
|---|---|---|
| `entity` | Legal/reporting entities | Stored as GUID-based elements; aliases provide readable names |
| `account` | Chart of accounts | Includes attributes such as code/name/type/account_type |
| `month` | Financial months and consolidators | Includes month elements plus groupings like quarter/half/YTD |
| `year` | Planning/reporting years | Used across source, planning, and reporting cubes |
| `version` | Scenario/version control | Typical members include actual, budget, forecast, prior_year |
| `contact` | Xero contact dimension | Used in detailed GL/source structures |
| `listed_share` | Securities dimension | Includes share/company alias attributes for lookup |
| `cashflow_activity` | Cashflow category dimension | Used for classification and summary reporting |
| `cost_object` | Cost allocation/planning structure | Used in planning intersections |
| `investec_account` | Portfolio account structure | Used in listed share holdings/transactions |
| `input_type` | Planning input behavior | Distinguishes calculated vs adjustment-style values |
| `listed_share_transaction_type` | Share transaction category | Buy/sell/dividend-style modeling intersections |

## Conventions

### 1) GUID-Based Entity Modeling

`entity` uses GUIDs as base elements rather than human-readable names.
Operationally this means:

- Lookups should use aliases/attributes when working with users
- Reports should map GUID to readable labels before presentation when possible

### 2) Consolidator Patterns

Many dimensions include rollup members such as `All_*` and time consolidators.
Examples:

- `All_Entity`
- Quarter and half-year consolidations in `month`
- YTD-style groupings

### 3) Measure-Dimension Pattern

Cubes often include a dedicated measure dimension (for example, `measure_*`) to isolate:

- Value semantics (amount, tax, debit, credit, etc.)
- Reporting outputs versus source fields

### 4) Layer Alignment

Dimensions are reused consistently across `src`, `cal`, `pln`, and `rpt` layers so transformations remain traceable.

## Retrieval Tips for RAG

- Prioritize alias-rich descriptions for `entity` and `listed_share`.
- Include both technical names and business labels in chunk text.
- Store layer context (`src`, `cal`, `pln`, `rpt`) in metadata for better retrieval precision.
