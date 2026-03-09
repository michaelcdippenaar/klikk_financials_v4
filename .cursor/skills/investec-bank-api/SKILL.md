---
name: investec-bank-api
description: Integrates and documents the Investec Private Banking (SA PB Account Information) API for accounts, balances, and transactions. Use when working with Investec bank transactions, Investec API, SA PB Account Information, or syncing Investec Private Bank data to PostgreSQL.
---

# Investec Private Banking API

## When to use this skill

Use when implementing or debugging Investec **bank** (current account) integration: syncing accounts/transactions, auth, or any code that calls the Investec Open API. This is separate from Investec **JSE/securities** data (holdings, share transactions) in the same app.

## Authentication

- **Token endpoint**: `POST {base_url}/identity/v2/oauth2/token`
- **Method**: OAuth2 client credentials.
- **Headers**:
  - `Authorization: Basic base64(client_id:client_secret)`
  - `x-api-key: <API_KEY>` (required; from Investec Developer Portal).
- **Body**: `application/x-www-form-urlencoded` with `grant_type=client_credentials`.
- **Response**: `access_token`, `expires_in` (seconds; typically 1799). Token is valid about 30 minutes; refresh before expiry.

**Base URLs**:
- Production: `https://openapi.investec.com`
- Sandbox: `https://openapisandbox.investec.com`

## Key endpoints

| Purpose | Method | Path |
|--------|--------|------|
| Get accounts | GET | `/za/pb/v1/accounts` |
| Get account balance | GET | `/za/pb/v1/accounts/{accountId}/balance` |
| Get account transactions | GET | `/za/pb/v1/accounts/{accountId}/transactions` |
| Get pending transactions | GET | `/za/pb/v1/accounts/{accountId}/pending-transactions` |

**Transactions query params** (all optional):
- `fromDate`, `toDate`: YYYY-MM-DD. Default: from = today − 180 days, to = today.
- `transactionType`: e.g. `FeesAndInterest`.
- `includePending`: set to include pending transactions in the main transactions response.

## Transaction payload (relevant fields)

- `accountId`, `type` (CREDIT/DEBIT), `transactionType`, `status` (POSTED/PENDING)
- `description`, `cardNumber`, `postedOrder`
- `postingDate`, `valueDate`, `actionDate`, `transactionDate` (date strings)
- `amount`, `runningBalance` (numbers)
- `uuid`: optional; only for **posted** Private Bank transactions. Format: `(last 5 of accountId) + (postingDate no hyphens) + (postedOrder padded to 7)`.

## Account payload

- `accountId`, `accountNumber`, `accountName`, `referenceName`, `productName`
- `kycCompliant`, `profileId`, `profileName`

## Project implementation

- **Settings**: `INVESTEC_BASE_URL`, `INVESTEC_CLIENT_ID`, `INVESTEC_CLIENT_SECRET`, `INVESTEC_API_KEY` (from env or .env).
- **Client**: `apps.investec.bank_api` — `get_access_token`, `fetch_accounts`, `fetch_transactions`, `transaction_to_model_data`.
- **Models**: `InvestecBankAccount`, `InvestecBankTransaction` in `apps.investec.models`.
- **Sync**: `python manage.py sync_investec_bank_transactions` (optional `--from-date`, `--to-date`, `--include-pending`, `--dry-run`).

## Full API reference

For full request/response schemas, all endpoints (beneficiaries, transfers, documents), and error codes, see **[database_dumps/openapi-2.json](database_dumps/openapi-2.json)** in this repo.
