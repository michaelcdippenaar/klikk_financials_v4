# Current Data Model Analysis

## Overview

This document analyzes the current data model used by klikk_financials_v3 to understand the detail level required for the migration away from the deprecated Journals API.

## Current Data Flow

```
Xero Journals API → XeroJournalsSource → create_journals_from_xero() → XeroJournals → get_account_balances() → XeroTrailBalance
```

## Key Models

### 1. XeroTransactionSource

Stores raw transaction data from various Xero endpoints (invoices, payments, bank transactions, etc.)

| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | The Xero organisation |
| transactions_id | CharField(51) | Unique transaction ID from Xero |
| transaction_source | CharField(51) | Type: 'BankTransaction', 'Invoice', 'Payment', 'CreditNote', 'Prepayment', 'Overpayment' |
| contact | FK(XeroContacts) | Associated contact |
| collection | JSONField | Raw JSON data from Xero API |

### 2. XeroJournalsSource

Stores raw journal data from Xero (both regular and manual journals).

| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | The Xero organisation |
| journal_id | CharField(51) | Unique journal ID |
| journal_number | IntegerField | Journal number |
| journal_type | CharField(20) | 'journal' or 'manual_journal' |
| collection | JSONField | Raw JSON with JournalLines |
| processed | BooleanField | Whether journal has been processed |

### 3. XeroJournals (Processed Journal Lines)

Stores processed journal line items - this is the detail level we need to replicate.

| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | The Xero organisation |
| journal_id | CharField(51) | Line ID (unique per organisation) |
| journal_number | IntegerField | Parent journal number |
| journal_type | CharField(20) | 'journal' or 'manual_journal' |
| account | FK(XeroAccount) | Account being debited/credited |
| transaction_source | FK(XeroTransactionSource) | Link to source transaction |
| journal_source | FK(XeroJournalsSource) | Link to journal source |
| date | DateTimeField | Transaction date |
| tracking1 | FK(XeroTracking) | First tracking category |
| tracking2 | FK(XeroTracking) | Second tracking category |
| description | TextField | Line description |
| reference | TextField | Reference text |
| amount | Decimal(30,2) | Line amount |
| tax_amount | Decimal(30,2) | Tax amount |

### 4. XeroTrailBalance (Aggregated)

Stores aggregated journal amounts by account/period.

| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | The Xero organisation |
| account | FK(XeroAccount) | Account |
| date | DateField | First day of month |
| year | IntegerField | Year |
| month | IntegerField | Month |
| fin_year | IntegerField | Financial year |
| fin_period | IntegerField | Financial period |
| contact | FK(XeroContacts) | Contact (for drill-down) |
| tracking1 | FK(XeroTracking) | First tracking category |
| tracking2 | FK(XeroTracking) | Second tracking category |
| amount | Decimal(30,2) | Aggregated amount |
| balance_to_date | Decimal(30,2) | YTD balance (P&L accounts) |

## Detail Level Required

The current model tracks the following dimensions for aggregation:

1. **Account-level totals** by year/month ✓
2. **Tracking category breakdown** (tracking1, tracking2) ✓  
3. **Contact breakdown** for balance sheet accounts ✓
4. **Source transaction link** for drill-down ✓

## Transaction to Journal Mapping

When replacing the Journals API, we need to understand how each transaction type creates journal entries:

### Invoice (ACCREC - Accounts Receivable)

```
Debit:  Accounts Receivable  (Total + Tax)
Credit: Revenue Account      (LineAmount - from each LineItem)
Credit: Tax Account          (TaxAmount - if applicable)
```

### Bill (ACCPAY - Accounts Payable)

```
Debit:  Expense/Asset Account (LineAmount - from each LineItem)
Debit:  Tax Account           (TaxAmount - if applicable)
Credit: Accounts Payable      (Total + Tax)
```

### Bank Transaction (SPEND)

```
Debit:  Expense Account    (LineAmount)
Credit: Bank Account       (Total)
```

### Bank Transaction (RECEIVE)

```
Debit:  Bank Account       (Total)
Credit: Revenue Account    (LineAmount)
```

### Payment (ACCRECPAYMENT - Customer Payment)

```
Debit:  Bank Account           (Amount)
Credit: Accounts Receivable    (Amount)
```

### Payment (ACCPAYPAYMENT - Supplier Payment)

```
Debit:  Accounts Payable   (Amount)
Credit: Bank Account       (Amount)
```

### Credit Note (ACCRECCREDIT - Customer)

```
Debit:  Revenue Account         (LineAmount)
Debit:  Tax Account             (TaxAmount)
Credit: Accounts Receivable     (Total)
```

### Credit Note (ACCPAYCREDIT - Supplier)

```
Debit:  Accounts Payable        (Total)
Credit: Expense/Asset Account   (LineAmount)
Credit: Tax Account             (TaxAmount)
```

### Manual Journal (UNCHANGED)

```
Manual journal endpoint remains available - no changes needed.
```

## API Endpoints to Use

### Currently Available (will use):

| Endpoint | Method | Notes |
|----------|--------|-------|
| `/Invoices` | `get_invoices()` | Sales and purchase invoices |
| `/Payments` | `get_payments()` | Customer and supplier payments |
| `/BankTransactions` | `get_bank_transactions()` | Spend and receive |
| `/CreditNotes` | `get_credit_notes()` | Already implemented |
| `/Prepayments` | `get_prepayments()` | Already implemented |
| `/Overpayments` | `get_overpayments()` | Already implemented |
| `/ManualJournals` | `get_manual_journals()` | Stays the same |
| `/Contacts` | `get_contacts()` | Already implemented |
| `/Accounts` | `get_accounts()` | Already implemented |
| `/TrackingCategories` | `get_tracking_categories()` | Already implemented |

### May Need for Completeness:

| Endpoint | Method | Notes |
|----------|--------|-------|
| `/PurchaseOrders` | `get_purchase_orders()` | For accruals |
| `/ExpenseClaims` | `get_expense_claims()` | Employee expenses |
| `/BankTransfers` | `get_bank_transfers()` | Inter-account transfers |

### Will be Deprecated (April 2026):

| Endpoint | Method | Notes |
|----------|--------|-------|
| `/Journals` | `get_journals()` | **BEING DEPRECATED** |

## System Account Codes

Need to map these system accounts for journal entry creation:

| Account Type | Code Pattern | Notes |
|-------------|--------------|-------|
| Accounts Receivable | `120` (typical) | Use account type = 'RECEIVABLE' |
| Accounts Payable | `200` (typical) | Use account type = 'PAYABLE' |
| Bank Accounts | Various | Use account type = 'BANK' |
| Tax Accounts | Various | Use `TaxType` from line items |

## Migration Strategy

1. **Keep XeroJournals model structure** - Same fields, same aggregation
2. **Replace data source** - From Journals API to Transaction APIs
3. **Create transaction processor** - Convert transactions to journal entries
4. **Validate with comparison tests** - Ensure totals match

## Files to Create

1. `apps/xero/xero_data/transaction_processor.py` - Convert transactions to journal entries
2. `apps/xero/xero_data/tests/test_journal_migration.py` - Comparison tests
3. `apps/xero/xero_data/comparison_utils.py` - Utilities for comparing results
