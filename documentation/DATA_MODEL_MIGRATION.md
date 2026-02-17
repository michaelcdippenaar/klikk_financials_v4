# Data Model Documentation - Journals to Transactions Migration

## Current Data Model Overview

The current system uses Xero's Journals API as the primary source for financial data, which is then processed into a Trail Balance for reporting.

### Data Flow Diagram

```
┌─────────────────────┐     ┌──────────────────────┐     ┌─────────────────┐     ┌────────────────────┐
│   Xero Journals     │     │   XeroJournals       │     │   XeroTrail     │     │   XeroBalance      │
│   API               │ ──▶ │   Source             │ ──▶ │   Balance       │ ──▶ │   Sheet            │
│   (Regular +        │     │   (Raw JSON)         │     │   (Aggregated)  │     │   (Cumulative)     │
│   Manual Journals)  │     │                      │     │                 │     │                    │
└─────────────────────┘     └──────────────────────┘     └─────────────────┘     └────────────────────┘
                                      │
                                      ▼
                            ┌──────────────────────┐
                            │   XeroJournals       │
                            │   (Processed Lines)  │
                            └──────────────────────┘
```

## Current Models

### 1. XeroTransactionSource

**Purpose**: Stores raw transaction data from Xero's transaction APIs.

**Fields**:
| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | Parent organisation |
| transactions_id | CharField(51) | Unique transaction ID from Xero |
| transaction_source | CharField(51) | Type: 'BankTransaction', 'Invoice', 'Payment' |
| contact | FK(XeroContacts) | Optional related contact |
| collection | JSONField | Raw JSON response from Xero |

**Unique Constraint**: (organisation, transactions_id)

**Supported Transaction Types**:
- `BankTransaction` - Bank transactions (spend/receive)
- `Invoice` - Sales invoices (ACCREC) and bills (ACCPAY)
- `Payment` - Payment records

### 2. XeroJournalsSource

**Purpose**: Stores raw journal data from Xero's Journals API before processing.

**Fields**:
| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | Parent organisation |
| journal_id | CharField(51) | Unique journal ID (JournalID or ManualJournalID) |
| journal_number | IntegerField | Journal sequence number |
| journal_type | CharField(20) | 'journal' or 'manual_journal' |
| collection | JSONField | Raw JSON from Xero API |
| processed | BooleanField | Whether processed to XeroJournals |

**Unique Constraint**: (organisation, journal_id, journal_type)

**Indexes**:
- (organisation, processed) - For processing unprocessed journals
- (organisation, journal_number) - For ordering
- (organisation, journal_type) - For filtering by type

### 3. XeroJournals

**Purpose**: Processed journal line items ready for aggregation into Trail Balance.

**Fields**:
| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | Parent organisation |
| journal_id | CharField(51) | Line ID (JournalLineID or generated for manual journals) |
| journal_number | IntegerField | Journal sequence number |
| journal_type | CharField(20) | 'journal' or 'manual_journal' |
| account | FK(XeroAccount) | Account this line affects |
| transaction_source | FK(XeroTransactionSource) | Optional: source transaction (Invoice, etc.) |
| journal_source | FK(XeroJournalsSource) | Link back to raw journal |
| date | DateTimeField | Transaction date |
| tracking1 | FK(XeroTracking) | First tracking category |
| tracking2 | FK(XeroTracking) | Second tracking category |
| description | TextField | Line description |
| reference | TextField | Journal reference/narration |
| amount | DecimalField(30,2) | Line amount (NetAmount for journals, LineAmount for manual) |
| tax_amount | DecimalField(30,2) | Tax amount |

**Unique Constraint**: (organisation, journal_id)

**Indexes**:
- (organisation, date) - Date-based queries
- (organisation, account) - Account-based queries
- (organisation, date, account) - Combined queries
- (date) - Global date queries
- (organisation, transaction_source) - Transaction lookups
- (organisation, journal_type) - Type filtering

### 4. XeroTrailBalance

**Purpose**: Aggregated monthly account balances for reporting.

**Fields**:
| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | Parent organisation |
| account | FK(XeroAccount) | Account |
| date | DateField | First of month |
| year | IntegerField | Calendar year |
| month | IntegerField | Calendar month |
| fin_year | IntegerField | Financial year |
| fin_period | IntegerField | Financial period |
| contact | FK(XeroContacts) | Optional: for receivables/payables breakdown |
| tracking1 | FK(XeroTracking) | First tracking category |
| tracking2 | FK(XeroTracking) | Second tracking category |
| amount | DecimalField(30,2) | Period amount |
| balance_to_date | DecimalField(30,2) | YTD for P&L accounts |

**Indexes**:
- (organisation, year, month) - Period queries
- (organisation, account, year, month) - Account/period queries
- (account, contact) - Receivables drill-down
- (organisation, account, contact) - Full drill-down
- (year, month) - Cross-org period queries

### 5. XeroBalanceSheet

**Purpose**: Cumulative balance sheet with running balance by account/contact.

**Fields**:
| Field | Type | Description |
|-------|------|-------------|
| organisation | FK(XeroTenant) | Parent organisation |
| date | DateField | Month date |
| year | IntegerField | Calendar year |
| month | IntegerField | Calendar month |
| account | FK(XeroAccount) | Account |
| contact | FK(XeroContacts) | Contact for receivables/payables |
| amount | DecimalField(30,2) | Period amount |
| balance | DecimalField(30,2) | Running balance |

**Unique Constraint**: (organisation, account, contact, date)

## Detail Level Requirements

The current model tracks the following dimensions that must be preserved:

### 1. Account-Level Totals
- Monthly totals by account
- Year/Month breakdown
- Financial year/period mapping

### 2. Tracking Category Breakdown
- Up to 2 tracking categories per line (tracking1, tracking2)
- Tracking options linked by option_id
- Full aggregation support in Trail Balance

### 3. Contact Breakdown
- Contact association for transactions
- Receivables/Payables drill-down capability
- Balance sheet by contact

### 4. Source Transaction Link
- Link to original transaction (Invoice, BankTransaction, etc.)
- Enables audit trail and drill-down
- Preserves source JSON data

### 5. Journal Type Separation
- Regular journals (from Journals API)
- Manual journals (from ManualJournals API)
- Ability to exclude manual journals from certain reports

## Processing Flow

### Current: Journals-Based Processing

```python
# 1. Fetch journals from API
xero_api.journals(load_all=False).get()    # Regular journals
xero_api.manual_journals(load_all=False).get()  # Manual journals

# 2. Store raw journal data
XeroJournalsSource.objects.create(...)

# 3. Process to journal lines
XeroJournalsSource.objects.create_journals_from_xero(organisation)

# 4. Aggregate to trail balance
journals = XeroJournals.objects.get_account_balances(organisation)
XeroTrailBalance.objects.consolidate_journals(organisation, journals)
```

### Proposed: Transaction-Based Processing

```python
# 1. Fetch transactions from API
xero_api.invoices().get()
xero_api.bank_transactions().get()
xero_api.payments().get()
xero_api.credit_notes().get()
xero_api.prepayments().get()
xero_api.overpayments().get()
xero_api.manual_journals().get()  # Still use manual journals

# 2. Store raw transaction data
XeroTransactionSource.objects.create_invoices_from_xero(...)

# 3. Process transactions to journal entries
# NEW: transaction_processor.py
process_transactions_to_journals(organisation)

# 4. Aggregate to trail balance (unchanged)
journals = XeroJournals.objects.get_account_balances(organisation)
XeroTrailBalance.objects.consolidate_journals(organisation, journals)
```

## Supporting Models (Metadata)

### XeroAccount
- Account chart from Xero
- Links to XeroBusinessUnits for grouping
- Fields: account_id, code, name, type, grouping, reporting_code

### XeroTracking
- Tracking category options
- Fields: option_id, name, option

### XeroContacts
- Customer/Supplier contacts
- Fields: contacts_id, name

### XeroTenant
- Organisation/tenant information
- Fields: tenant_id, tenant_name

## Key Considerations for Migration

1. **Preserve Tracking Categories**: Transactions have tracking at line item level
2. **Preserve Contact Association**: Critical for receivables/payables
3. **Maintain Journal Type Flag**: Keep ability to identify manual journals
4. **Handle Different Transaction Types**: Each transaction type has different structure:
   - Invoices: ACCREC (sales) vs ACCPAY (purchases)
   - Bank Transactions: SPEND vs RECEIVE
   - Credit Notes: Applied to invoices
   - Prepayments/Overpayments: Special handling

5. **Account Mapping**:
   - Regular transactions link accounts by AccountCode in LineItems
   - Need to derive receivables/payables accounts from transaction type

6. **Date Handling**:
   - Different date fields: Date, DateString, DueDate, InvoiceDate, etc.
   - Need consistent date extraction
