# Dividend Forecast Budget — Process & Concepts

## Overview

The dividend forecast budget is a workflow that adjusts the TM1 budget when companies declare their dividends. TM1 rules calculate a **base DPS** (dividends per share) from historical patterns, and when a company's actual declared dividend differs from this base, an **adjustment** is written to correct the forecast.

## Key Concepts & Terminology

### Dividend Lifecycle (JSE / South Africa)

| Term | Definition |
|------|-----------|
| **Declaration Date** | When the company announces the dividend amount. |
| **Last Day to Trade (LDT)** | The last trading day you can buy shares and still qualify for the dividend. Usually one business day before the ex-date. |
| **Ex-Dividend Date (Ex-Date)** | The first day the stock trades WITHOUT the dividend. If you buy on/after this date, you do NOT receive the dividend. The stock price typically drops by approximately the dividend amount on this day. |
| **Record Date** | The date the company checks its shareholder register. Usually one business day after the ex-date. |
| **Payment Date** | When the dividend cash is actually paid into your account. Typically 1-4 weeks after the ex-date. **This is the month used for TM1 budget entries.** |

### TM1 Budget Structure

| Dimension | Description | Example Values |
|-----------|-------------|----------------|
| **Cube** | `listed_share_pln_forecast` | — |
| **year** | Calendar year | `2026` |
| **month** | Month element (payment month) | `Jan`, `Feb`, ..., `Dec` |
| **version** | Budget version | `budget` |
| **entity** | Company entity GUID | `41ebfa0e-012e-4ff1-82ba-a9a7585c536c` (Klikk Pty Ltd) |
| **listed_share** | Share code element | `ABG`, `FSR`, `SBK`, `KIO` |
| **listed_share_transaction_type** | Transaction type (see below) | `Dividend`, `Special Dividend`, `Foreign Dividend` |
| **input_type** | Input category | `calculated`, `adjustment`, `adjustment_declared_dividend`, `All_Input_Type` |
| **measure** | What's being measured | `dividends_per_share`, `share_quantity`, `dividends_amount` |

### Transaction Type Elements

| Element | Type | Description |
|---------|------|-------------|
| `All_Listed_Share_Transaction_Type` | Consolidator | Rolls up everything. **Read-only** — cannot write to this. |
| `All Dividends` | Consolidator | Rolls up Dividend + Special Dividend + Foreign Dividend. **Read-only.** |
| `Dividend` | Leaf | Regular domestic dividend. **Writable.** |
| `Special Dividend` | Leaf | One-off special dividend. **Writable.** |
| `Foreign Dividend` | Leaf | International/foreign dividend. **Writable.** |
| `All Trades` | Consolidator | Rolls up Buy + Sell. Read-only. |
| `Buy`, `Sell` | Leaf | Share purchase/sale transactions. |
| `Broker Fee`, `Fee`, `Dividend Tax` | Leaf | Cost elements. |

### Input Types

| Input Type | Description |
|-----------|-------------|
| `calculated` | Base value computed by TM1 rules from historical dividend patterns. |
| `adjustment` | Manual adjustment (general purpose). |
| `adjustment_declared_dividend` | Adjustment for a declared dividend — the delta between declared DPS and calculated base DPS. |
| `All_Input_Type` | Consolidator that sums calculated + all adjustments = final forecast DPS. |

### Dividend Categories

| Category | Budgeted? | TM1 Element | Description |
|----------|-----------|-------------|-------------|
| `regular` | Yes | `Dividend` | Standard domestic dividend. Base is calculated by TM1 rules. Adjustment = declared - base. |
| `foreign` | Yes | `Foreign Dividend` | International dividend (non-JSE shares like GOOG, GS). Base is also calculated. Same adjustment formula. |
| `special` | **No** | `Special Dividend` | One-off special distribution. NOT budgeted for. Only written to TM1 once declared, and the auto-workflow skips these. |

### Shares Classification

- **Domestic (JSE)**: Symbols ending in `.JO` (e.g., `ABG.JO`, `FSR.JO`). Exchange: JNB. Currency: ZAR/ZAc.
- **Foreign/International**: Symbols without `.JO` suffix (e.g., `GOOG`, `GS`, `TSLA`). Listed on NYSE/NASDAQ. Currency: USD.
- **Special dividends**: Cannot be auto-detected from yfinance. Must be manually classified via the UI or Django admin.

## Adjustment Formula

```
base_dps = All_Input_Type_DPS - current_adjustment_declared_dividend_DPS
new_adjustment = declared_dps - base_dps
```

**Example (FSR — FirstRand):**
- TM1 calculated base DPS for April: 2.19 (from rules)
- Declared dividend: 2.59 ZAR
- Adjustment = 2.59 - 2.19 = **0.40** written to `adjustment_declared_dividend`
- Resulting total DPS = 2.19 + 0.40 = 2.59

### Month Selection: Payment Date, NOT Ex-Date

The TM1 month for an adjustment is determined by the **payment date** (when cash is received), not the ex-dividend date.

**Example (FSR):**
- Ex-date: March 31, 2026
- Payment date: April 7, 2026
- TM1 month: **April** (not March)

If no payment date is available (yfinance sometimes doesn't provide it), the ex-date is used as fallback.

### Zero Base (Recently Purchased Shares)

If a share was purchased recently, TM1 may have no historical dividend data, resulting in:
- `calculated` DPS = 0
- `adjustment` = 0
- `All_Input_Type` DPS = 0

In this case, the full declared DPS becomes the adjustment:
```
base_dps = 0 - 0 = 0
new_adjustment = declared_dps - 0 = declared_dps
```

The system logs a "zero base" warning and the UI shows a "Zero base (new share?)" indicator.

## Currency Notes

- **JSE shares (`.JO`)**: yfinance reports currency as `ZAc` (South African cents), but `lastDividendValue` actually returns **ZAR** (rands). Historical `.dividends` series returns **cents**. TM1 stores values in **ZAR**.
- **Foreign shares**: Values are in the native currency (USD for US stocks). TM1 stores in the same currency.
- **Important**: Do NOT convert `lastDividendValue` from cents to rands — it's already in rands despite the `ZAc` currency label.

## Workflow Steps

### 1. Check yfinance for declared dividends
- Queries yfinance `ticker.info` for all held shares (symbols with share_code mapping)
- Extracts: `exDividendDate`, `lastDividendValue`, `dividendRate`, `dividendDate` (payment date)
- Auto-detects foreign dividends (non-`.JO` symbols)
- Creates `DividendCalendar` entries for new declarations
- Runs concurrently (8 workers) for speed

### 2. Write TM1 adjustments for pending entries
- Finds all `DividendCalendar` entries where `tm1_adjustment_written = false` and `status = 'declared'`
- **Skips special dividends** — they are not auto-budgeted
- For each entry:
  - Uses **payment_date** month (or ex_date as fallback) for TM1 coordinates
  - Routes to correct TM1 transaction type leaf element based on `dividend_category`
  - Reads current TM1 values, computes adjustment, writes to TM1
  - Marks entry as written with adjustment value and timestamp

### 3. Verify TM1 values match
- Reads TM1 for all written entries
- Compares TM1 `adjustment_declared_dividend` value against DB `tm1_adjustment_value`
- Marks entries as verified or flags mismatches

## Data Model

### DividendCalendar (Django)
```
symbol              → ForeignKey(Symbol)
declaration_date    → DateField (nullable)
ex_dividend_date    → DateField (unique with symbol)
record_date         → DateField (nullable)
payment_date        → DateField (nullable)
amount              → DecimalField (declared DPS)
currency            → CharField
status              → 'declared' | 'paid' | 'estimated'
dividend_category   → 'regular' | 'special' | 'foreign'
source              → 'yfinance' | 'manual'
tm1_adjustment_written → BooleanField
tm1_adjustment_value   → DecimalField (the delta written)
tm1_written_at         → DateTimeField
tm1_verified           → BooleanField
tm1_verified_at        → DateTimeField
last_checked_at        → DateTimeField
```

### Symbol → Share Code Mapping
```
Symbol.symbol = 'ABG.JO'  →  InvestecJseShareNameMapping.share_code = 'ABG'
Symbol.symbol = 'GOOG'    →  InvestecJseShareNameMapping.share_code = 'UNK_ALPHABETINCC'
```

The `share_code` is the TM1 `listed_share` dimension element name.

## API Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/financial-investments/dividend-calendar/` | List calendar entries (filter: status, pending_tm1) |
| POST | `/api/financial-investments/dividend-calendar/check/` | Check yfinance for all shares |
| POST | `/api/financial-investments/dividend-calendar/update-category/` | Change dividend_category (body: id, dividend_category) |
| GET | `/api/financial-investments/dividend-forecast/<share_code>/?year=&month=` | Read TM1 forecast |
| POST | `/api/financial-investments/dividend-forecast/adjust/` | Write TM1 adjustment (body: share_code, declared_dps, year, month, confirm, dividend_category) |
| POST | `/api/financial-investments/dividend-forecast/adjust-pending/` | Batch write all pending |
| POST | `/api/financial-investments/dividend-forecast/verify/` | Verify TM1 matches DB |

## File Locations

| File | Purpose |
|------|---------|
| `apps/ai_agent/skills/dividend_forecast.py` | Core logic: TM1 read/write, yfinance check, background job |
| `apps/financial_investments/models.py` | DividendCalendar model |
| `apps/financial_investments/views.py` | API views for workflow |
| `apps/financial_investments/urls.py` | URL routing |
| `apps/financial_investments/admin.py` | Django admin config |
| `klikk-portal/src/pages/DividendForecast.vue` | Frontend UI (3 tabs: Calendar, Forecast, Workflow) |
| `klikk-portal/src/api/endpoints.js` | Frontend API functions |
| `klikk-portal/src/utils/constants.js` | API endpoint constants |

## Background Job

A daily async loop (`dividend_calendar_loop`) runs automatically:
1. Checks yfinance for all held shares
2. Writes TM1 adjustments for any pending entries
3. Logs results

Configured in `dividend_forecast.py` with a 24-hour interval and 30-second startup delay.
