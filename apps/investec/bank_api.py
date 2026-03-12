"""
Investec Private Banking API client (SA PB Account Information).

Auth: OAuth2 client_credentials with Basic Auth (client_id:client_secret) + x-api-key header.
Token valid 30 minutes. Base URL: https://openapi.investec.com or sandbox.
"""

import base64
import logging
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# In-memory token cache keyed by client_id: { client_id: (access_token, expires_at_epoch) }
_token_cache: dict[str, tuple[str, float]] = {}


def get_access_token(
    base_url: str,
    client_id: str,
    client_secret: str,
    api_key: str,
    buffer_seconds: int = 300,
) -> str:
    """
    Obtain OAuth2 access token. Uses client_credentials flow with Basic Auth + x-api-key.
    Caches per client_id and refreshes when within buffer_seconds of expiry (default 5 min).
    """
    now = time.time()
    cached = _token_cache.get(client_id)
    if cached is not None:
        token, expires = cached
        if expires > now + buffer_seconds:
            return token
    token_url = f"{base_url.rstrip('/')}/identity/v2/oauth2/token"
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "x-api-key": api_key,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}
    resp = requests.post(token_url, headers=headers, data=data, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    access_token = body["access_token"]
    expires_in = int(body.get("expires_in", 1799))
    _token_cache[client_id] = (access_token, now + expires_in)
    return access_token


def fetch_accounts(
    base_url: str,
    access_token: str,
) -> list[dict[str, Any]]:
    """GET /za/pb/v1/accounts. Returns list of account objects."""
    url = f"{base_url.rstrip('/')}/za/pb/v1/accounts"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 401:
        raise ValueError("Unauthorized: token may have expired")
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("accounts", [])


def fetch_transactions(
    base_url: str,
    access_token: str,
    account_id: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    transaction_type: Optional[str] = None,
    include_pending: bool = False,
) -> list[dict[str, Any]]:
    """
    GET /za/pb/v1/accounts/{accountId}/transactions.
    from_date/to_date as date objects; API expects YYYY-MM-DD.
    Default per API: from = today - 180 days, to = today.
    Returns only the first response page; use fetch_all_transactions for full history.
    """
    url = f"{base_url.rstrip('/')}/za/pb/v1/accounts/{account_id}/transactions"
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict[str, Any] = {}
    if from_date is not None:
        params["fromDate"] = from_date.isoformat()
    if to_date is not None:
        params["toDate"] = to_date.isoformat()
    if transaction_type:
        params["transactionType"] = transaction_type
    if include_pending:
        params["includePending"] = "true"
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    if resp.status_code == 401:
        raise ValueError("Unauthorized: token may have expired")
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("transactions", [])


def fetch_all_transactions(
    base_url: str,
    access_token: str,
    account_id: str,
    from_date: date,
    to_date: date,
    transaction_type: Optional[str] = None,
    include_pending: bool = False,
    chunk_months: bool = True,
) -> list[dict[str, Any]]:
    """
    Fetch all transactions in the date range. If chunk_months is True (default),
    requests one month at a time to work around API limits on result set size.
    """
    if not chunk_months:
        return fetch_transactions(
            base_url, access_token, account_id,
            from_date=from_date, to_date=to_date,
            transaction_type=transaction_type, include_pending=include_pending,
        )
    all_txns: list[dict[str, Any]] = []
    # Iterate month by month to avoid API result set limits
    current = date(from_date.year, from_date.month, 1)
    end = to_date
    while current <= end:
        month_start = current
        if current.month == 12:
            month_end = date(current.year, 12, 31)
        else:
            month_end = date(current.year, current.month + 1, 1) - timedelta(days=1)
        if month_end > end:
            month_end = end
        chunk = fetch_transactions(
            base_url, access_token, account_id,
            from_date=month_start, to_date=month_end,
            transaction_type=transaction_type, include_pending=include_pending,
        )
        all_txns.extend(chunk)
        # Next month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return all_txns


def parse_api_date(value: Any) -> Optional[date]:
    """Parse API date string (YYYY-MM-DD or ISO date-time) to date."""
    if value is None:
        return None
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def transaction_to_model_data(txn: dict[str, Any]) -> dict[str, Any]:
    """Map API transaction object to InvestecBankTransaction model fields."""
    return {
        "type": txn.get("type") or "",
        "transaction_type": (txn.get("transactionType") or "")[:40],
        "status": txn.get("status") or "POSTED",
        "description": (txn.get("description") or "")[:255],
        "card_number": (txn.get("cardNumber") or "")[:40],
        "posted_order": txn.get("postedOrder"),
        "posting_date": parse_api_date(txn.get("postingDate")),
        "value_date": parse_api_date(txn.get("valueDate")),
        "action_date": parse_api_date(txn.get("actionDate")),
        "transaction_date": parse_api_date(txn.get("transactionDate")),
        "amount": Decimal(str(txn.get("amount", 0))),
        "running_balance": (
            Decimal(str(txn["runningBalance"])) if txn.get("runningBalance") is not None else None
        ),
        "uuid": (txn.get("uuid") or "").strip() or None,
    }
