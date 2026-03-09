"""
Shared Investec Private Bank sync logic. Used by the management command and the API.
Updates InvestecBankSyncLog.last_synced_at on success (when not dry_run).
"""

import hashlib
from datetime import date, timedelta
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.investec.bank_api import (
    fetch_accounts,
    fetch_all_transactions,
    get_access_token,
    transaction_to_model_data,
)
from apps.investec.models import InvestecBankAccount, InvestecBankSyncLog, InvestecBankTransaction


def run_investec_bank_sync(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    include_pending: bool = False,
    account_filter: Optional[str] = None,
    dry_run: bool = False,
    update_sync_log: bool = True,
) -> dict[str, Any]:
    """
    Sync Investec bank accounts and transactions from the API.
    If from_date/to_date are None, uses to_date=today, from_date=today-180.
    When update_sync_log is True and not dry_run, sets InvestecBankSyncLog.last_synced_at to now on success.
    Returns dict: { created, updated, last_synced_at?, error? }
    """
    base_url = getattr(settings, "INVESTEC_BASE_URL", "").strip() or "https://openapi.investec.com"
    client_id = getattr(settings, "INVESTEC_CLIENT_ID", "") or ""
    client_secret = getattr(settings, "INVESTEC_CLIENT_SECRET", "") or ""
    api_key = getattr(settings, "INVESTEC_API_KEY", "") or ""

    if not all([client_id, client_secret, api_key]):
        return {
            "created": 0,
            "updated": 0,
            "error": "Missing Investec API credentials. Set INVESTEC_CLIENT_ID, INVESTEC_CLIENT_SECRET, and INVESTEC_API_KEY.",
        }

    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=180)

    result = {"created": 0, "updated": 0}

    try:
        token = get_access_token(base_url, client_id, client_secret, api_key)
        accounts_data = fetch_accounts(base_url, token)
        if not accounts_data:
            return {**result, "error": "No accounts returned from API."}

        if account_filter:
            account_filter_str = str(account_filter).strip()
            accounts_data = [
                a for a in accounts_data
                if str(a.get("accountId") or "") == account_filter_str
                or str(a.get("accountNumber") or "") == account_filter_str
            ]
            if not accounts_data:
                return {**result, "error": f"No account found with id/number: {account_filter}."}

        seen_ids = set()
        accounts_data = [a for a in accounts_data if a["accountId"] not in seen_ids and not seen_ids.add(a["accountId"])]

        if not dry_run:
            for acc in accounts_data:
                InvestecBankAccount.objects.update_or_create(
                    account_id=acc["accountId"],
                    defaults={
                        "account_number": acc.get("accountNumber") or "",
                        "account_name": (acc.get("accountName") or "")[:70],
                        "reference_name": (acc.get("referenceName") or "")[:70],
                        "product_name": (acc.get("productName") or "")[:70],
                        "kyc_compliant": bool(acc.get("kycCompliant")),
                        "profile_id": (acc.get("profileId") or "")[:70],
                        "profile_name": (acc.get("profileName") or "")[:70],
                    },
                )

        total_created = 0
        total_updated = 0
        for acc in accounts_data:
            account_id = acc["accountId"]
            txns = fetch_all_transactions(
                base_url,
                token,
                account_id,
                from_date=from_date,
                to_date=to_date,
                include_pending=include_pending,
            )

            if dry_run:
                continue

            bank_account = InvestecBankAccount.objects.get(account_id=account_id)
            with transaction.atomic():
                deleted = InvestecBankTransaction.objects.filter(
                    account=bank_account,
                    uuid__isnull=True,
                    fallback_key__isnull=True,
                    posted_order__in=(None, 0),
                ).delete()[0]
                for txn in txns:
                    data = transaction_to_model_data(txn)
                    uuid_val = (data.get("uuid") or "").strip() or None
                    posting_date = data.get("posting_date")
                    posted_order = data.get("posted_order")
                    if posted_order is not None and not isinstance(posted_order, int):
                        try:
                            posted_order = int(posted_order)
                            data["posted_order"] = posted_order
                        except (TypeError, ValueError):
                            posted_order = None

                    use_fallback = not uuid_val and (posted_order is None or posted_order == 0)
                    fallback_key = None
                    if use_fallback:
                        parts = (
                            str(data.get("transaction_date") or ""),
                            str(data.get("value_date") or ""),
                            str(data.get("action_date") or ""),
                            str(data.get("amount") or ""),
                            (data.get("description") or "")[:255],
                        )
                        fallback_key = hashlib.sha256("|".join(parts).encode()).hexdigest()
                        data["fallback_key"] = fallback_key
                        data["posted_order"] = None

                    if uuid_val:
                        obj, created = InvestecBankTransaction.objects.update_or_create(
                            uuid=uuid_val,
                            defaults={**data, "account": bank_account},
                        )
                    elif fallback_key:
                        obj, created = InvestecBankTransaction.objects.update_or_create(
                            account=bank_account,
                            fallback_key=fallback_key,
                            defaults=data,
                        )
                    elif posting_date is not None and posted_order is not None:
                        obj, created = InvestecBankTransaction.objects.update_or_create(
                            account=bank_account,
                            posting_date=posting_date,
                            posted_order=posted_order,
                            defaults=data,
                        )
                    else:
                        InvestecBankTransaction.objects.create(account=bank_account, **data)
                        created = True

                    if created:
                        total_created += 1
                    else:
                        total_updated += 1

        result["created"] = total_created
        result["updated"] = total_updated

        if update_sync_log and not dry_run:
            now = timezone.now()
            log, _ = InvestecBankSyncLog.objects.get_or_create(key="default", defaults={"last_synced_at": now})
            log.last_synced_at = now
            log.save(update_fields=["last_synced_at"])
            result["last_synced_at"] = now.isoformat()

        return result

    except Exception as e:
        return {**result, "error": str(e)}
