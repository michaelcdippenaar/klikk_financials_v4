# Investec SA PB API – Endpoint reference

Condensed from the OpenAPI spec. Full schema: [database_dumps/openapi-2.json](../../../database_dumps/openapi-2.json).

## Auth

- **POST** `{base}/identity/v2/oauth2/token`
  - Headers: `Authorization: Basic <base64(client_id:client_secret)>`, `x-api-key: <key>`
  - Body: `grant_type=client_credentials` (x-www-form-urlencoded)
  - Response: `{ "access_token": "...", "expires_in": 1799 }`

## Account information

- **GET** `/za/pb/v1/accounts`
  - Response: `data.accounts[]` with accountId, accountNumber, accountName, referenceName, productName, kycCompliant, profileId, profileName

- **GET** `/za/pb/v1/accounts/{accountId}/balance`
  - Response: `data` with accountId, currentBalance, availableBalance, budgetBalance, straightBalance, cashBalance, currency

- **GET** `/za/pb/v1/accounts/{accountId}/transactions`
  - Query: fromDate, toDate (YYYY-MM-DD), transactionType, includePending (bool)
  - Response: `data.transactions[]` with accountId, type (CREDIT/DEBIT), transactionType, status (POSTED/PENDING), description, cardNumber, postedOrder, postingDate, valueDate, actionDate, transactionDate, amount, runningBalance, uuid (optional)

- **GET** `/za/pb/v1/accounts/{accountId}/pending-transactions`
  - Response: `data.PendingTransaction[]` (or similar; see OpenAPI for exact key) with accountId, type, status, description, transactionDate, amount

## Errors

- 400 Bad Request, 401 Unauthorized, 403 Forbidden, 429 Too Many Requests, 500 Internal Server Error. On 429, retry after backoff.
