# Getting Supporting Documents from the Database

Instructions for retrieving Xero supporting documents (attachments such as invoices, receipts, PDFs) from the Klikk Financials V4 database.

---

## Overview

Supporting documents are stored in the `xero_data_xerodocument` table, linked to transactions via `xero_data_xerotransactionsource`. Each document represents a file attachment imported from Xero (e.g. a PDF invoice, a receipt image, a credit note PDF).

### Data Model

```
XeroTransactionSource (xero_data_xerotransactionsource)
  ├── transactions_id       -- Xero transaction ID (e.g. InvoiceID)
  ├── transaction_source    -- Type: Invoice, CreditNote, BankTransaction, etc.
  ├── organisation_id       -- FK → xero_core_xerotenant
  └── contact_id            -- FK → xero_metadata_xerocontacts
       │
       ▼
XeroDocument (xero_data_xerodocument)
  ├── id                    -- Primary key
  ├── file_name             -- Original filename (e.g. "INV-0042.pdf")
  ├── file                  -- File path on disk (relative to XERO_DOCUMENTS_ROOT)
  ├── content_type          -- MIME type (e.g. "application/pdf", "image/png")
  ├── xero_attachment_id    -- Xero AttachmentID
  ├── organisation_id       -- FK → xero_core_xerotenant
  ├── transaction_source_id -- FK → xero_data_xerotransactionsource
  ├── created_at
  └── updated_at
```

### File Storage Location

Documents are stored on disk at the path configured by `XERO_DOCUMENTS_ROOT` (or `MEDIA_ROOT` if not set).

| Environment | Setting | Default Value |
|-------------|---------|---------------|
| Production / Docker | `XERO_DOCUMENTS_ROOT` env var | `/var/data/klikk_financials_v4/xero_documents` |
| Development | `XERO_DOCUMENTS_ROOT` in `.env` | `/var/data/klikk_financials_v4/xero_documents` |
| Fallback | `MEDIA_ROOT` | `<project_root>/media` |

Files are saved in the directory structure:
```
{XERO_DOCUMENTS_ROOT}/xero_documents/{organisation_id}/{transaction_type}/{transaction_id}/{filename}
```

---

## Method 1: Direct SQL Queries (psql)

### Connect to the database

```bash
# Development (remote DB on 192.168.1.235)
psql -h 192.168.1.235 -U klikk_user -d klikk_financials_v4

# Production (local DB)
psql -h 127.0.0.1 -U klikk_user -d klikk_financials_v4
```

### List all supporting documents

```sql
SELECT
    d.id,
    d.file_name,
    d.content_type,
    d.file,
    d.xero_attachment_id,
    d.created_at,
    ts.transactions_id,
    ts.transaction_source AS transaction_type,
    d.organisation_id
FROM xero_data_xerodocument d
JOIN xero_data_xerotransactionsource ts
    ON d.transaction_source_id = ts.id
ORDER BY d.created_at DESC;
```

### Get documents for a specific transaction (by Xero ID)

```sql
SELECT
    d.id,
    d.file_name,
    d.content_type,
    d.file AS file_path,
    d.xero_attachment_id,
    ts.transaction_source AS transaction_type
FROM xero_data_xerodocument d
JOIN xero_data_xerotransactionsource ts
    ON d.transaction_source_id = ts.id
WHERE ts.transactions_id = '<XERO_TRANSACTION_ID>';
```

### Get documents for a specific tenant

```sql
SELECT
    d.id,
    d.file_name,
    d.content_type,
    d.file AS file_path,
    ts.transactions_id,
    ts.transaction_source AS transaction_type,
    c.name AS contact_name
FROM xero_data_xerodocument d
JOIN xero_data_xerotransactionsource ts
    ON d.transaction_source_id = ts.id
LEFT JOIN xero_metadata_xerocontacts c
    ON ts.contact_id = c.contacts_id
WHERE d.organisation_id = '<TENANT_ID>'
ORDER BY d.created_at DESC;
```

### Get documents by transaction type (e.g. only Invoices)

```sql
SELECT
    d.id,
    d.file_name,
    d.content_type,
    d.file AS file_path,
    ts.transactions_id
FROM xero_data_xerodocument d
JOIN xero_data_xerotransactionsource ts
    ON d.transaction_source_id = ts.id
WHERE ts.transaction_source = 'Invoice'
  AND d.organisation_id = '<TENANT_ID>'
ORDER BY d.created_at DESC;
```

Supported `transaction_source` values: `Invoice`, `CreditNote`, `BankTransaction`.

### Count documents per tenant and type

```sql
SELECT
    d.organisation_id,
    t.tenant_name,
    ts.transaction_source AS type,
    COUNT(*) AS document_count
FROM xero_data_xerodocument d
JOIN xero_data_xerotransactionsource ts
    ON d.transaction_source_id = ts.id
JOIN xero_core_xerotenant t
    ON d.organisation_id = t.tenant_id
GROUP BY d.organisation_id, t.tenant_name, ts.transaction_source
ORDER BY t.tenant_name, ts.transaction_source;
```

### Get documents linked to journal entries

To trace from a journal line back to its supporting document:

```sql
SELECT
    j.journal_id,
    j.date,
    j.description,
    j.amount,
    a.code AS account_code,
    a.name AS account_name,
    d.file_name,
    d.content_type,
    d.file AS file_path
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a
    ON j.account_id = a.account_id
JOIN xero_data_xerotransactionsource ts
    ON j.transaction_source_id = ts.transactions_id
JOIN xero_data_xerodocument d
    ON d.transaction_source_id = ts.id
WHERE j.organisation_id = '<TENANT_ID>'
  AND j.date >= '2025-01-01'
ORDER BY j.date DESC, d.file_name;
```

---

## Method 2: REST API

### Sync documents from Xero (import)

Before you can retrieve documents, they must be synced from Xero:

```bash
# Sync all supported document types for a tenant
curl -X POST http://localhost:8001/xero/data/sync/documents/ \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "<TENANT_ID>"}'

# Sync only specific transaction types
curl -X POST http://localhost:8001/xero/data/sync/documents/ \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "<TENANT_ID>",
    "types": ["Invoice", "CreditNote"]
  }'

# Sync specific transactions by ID
curl -X POST http://localhost:8001/xero/data/sync/documents/ \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "<TENANT_ID>",
    "transaction_ids": ["abc-123-def", "ghi-456-jkl"]
  }'
```

**Response:**
```json
{
  "success": true,
  "message": "Synced 12 document(s) for tenant <TENANT_ID>",
  "synced": 12,
  "errors": [],
  "skipped": 0
}
```

### Get documents for a transaction

```bash
# List documents attached to a specific transaction
curl http://localhost:8001/xero/data/documents/by-transaction/<TRANSACTION_ID>/

# Filter by tenant
curl http://localhost:8001/xero/data/documents/by-transaction/<TRANSACTION_ID>/?tenant_id=<TENANT_ID>
```

**Response:**
```json
[
  {
    "id": 1,
    "file_name": "INV-0042.pdf",
    "content_type": "application/pdf",
    "url": "http://localhost:8001/media/xero_documents/.../INV-0042.pdf",
    "transaction_id": "abc-123-def",
    "transaction_source": "Invoice"
  }
]
```

The `url` field contains a direct download link to the file.

---

## Method 3: Django ORM (Python shell or management commands)

### Start a Django shell

```bash
cd /home/mc/apps/klikk_financials_v4
source venv/bin/activate
python manage.py shell
```

### Query documents

```python
from apps.xero.xero_data.models import XeroDocument, XeroTransactionSource

# All documents
docs = XeroDocument.objects.all()

# Documents for a specific tenant
docs = XeroDocument.objects.filter(organisation_id='<TENANT_ID>')

# Documents for a specific transaction
docs = XeroDocument.objects.filter(
    transaction_source__transactions_id='<XERO_TRANSACTION_ID>'
)

# Documents by type (e.g. Invoices only)
docs = XeroDocument.objects.filter(
    transaction_source__transaction_source='Invoice',
    organisation_id='<TENANT_ID>',
)

# With related data (efficient queries)
docs = XeroDocument.objects.select_related(
    'transaction_source', 'organisation'
).filter(organisation_id='<TENANT_ID>')

# Print results
for d in docs:
    print(f"{d.file_name} | {d.content_type} | {d.transaction_source.transactions_id} | {d.file.url}")
```

### Access the actual file

```python
doc = XeroDocument.objects.first()

# File path on disk
print(doc.file.path)       # e.g. /var/data/klikk_financials_v4/xero_documents/...

# URL for serving
print(doc.file.url)        # e.g. /media/xero_documents/...

# Read file content
with doc.file.open('rb') as f:
    content = f.read()
```

### Trigger a document sync programmatically

```python
from apps.xero.xero_data.document_sync import sync_documents_for_tenant

result = sync_documents_for_tenant(
    tenant_id='<TENANT_ID>',
    source_types=['Invoice', 'CreditNote', 'BankTransaction'],
)
print(result)
# {'success': True, 'message': 'Synced 12 document(s) for tenant ...', 'synced': 12, 'errors': [], 'skipped': 0}
```

---

## Method 4: Django Admin

Documents are registered in Django Admin via `XeroDocumentAdmin`. Navigate to:

```
http://localhost:8001/admin/xero_data/xerodocument/
```

This provides a UI to browse, search, and filter documents by tenant, transaction type, and filename.

---

## Key Tables Reference

| Table | Purpose |
|-------|---------|
| `xero_data_xerodocument` | Stored document files and metadata |
| `xero_data_xerotransactionsource` | Parent transactions that documents attach to |
| `xero_data_xerojournals` | Journal entries (link to transaction sources) |
| `xero_core_xerotenant` | Xero tenants (organisations) |
| `xero_metadata_xerocontacts` | Contacts on transactions |

## Prerequisites for Syncing

1. **Xero OAuth scopes** must include `accounting.attachments` or `accounting.attachments.read`
2. **Xero credentials** must be configured in `xero_auth` (valid OAuth2 tokens)
3. **Transactions must exist** in `xero_data_xerotransactionsource` (run journal sync first)
4. **`XERO_DOCUMENTS_ROOT`** must be writable by the Django process
