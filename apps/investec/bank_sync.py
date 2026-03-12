"""
Shared Investec Private Bank sync logic. Used by the management command and the API.
Updates InvestecBankSyncLog.last_synced_at on success (when not dry_run).
Supports multiple credential profiles (settings.INVESTEC_PROFILES).
"""

import hashlib
import logging
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

logger = logging.getLogger(__name__)


def _sync_single_profile(
    profile: dict,
    base_url: str,
    from_date: date,
    to_date: date,
    include_pending: bool,
    account_filter: Optional[str],
    dry_run: bool,
) -> dict[str, Any]:
    """Sync accounts and transactions for a single credential profile."""
    client_id = profile["client_id"]
    client_secret = profile["client_secret"]
    api_key = profile["api_key"]

    if not all([client_id, client_secret, api_key]):
        return {"created": 0, "updated": 0, "error": f"Incomplete credentials for profile (client_id={client_id[:8]}...)."}

    token = get_access_token(base_url, client_id, client_secret, api_key)
    accounts_data = fetch_accounts(base_url, token)
    if not accounts_data:
        return {"created": 0, "updated": 0, "accounts": 0}

    if account_filter:
        account_filter_str = str(account_filter).strip()
        accounts_data = [
            a for a in accounts_data
            if str(a.get("accountId") or "") == account_filter_str
            or str(a.get("accountNumber") or "") == account_filter_str
        ]

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
            InvestecBankTransaction.objects.filter(
                account=bank_account,
                uuid__isnull=True,
                fallback_key__isnull=True,
                posted_order__in=(None, 0),
            ).delete()
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
                        str(account_id),
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
                    existing = InvestecBankTransaction.objects.filter(
                        account=bank_account,
                        transaction_date=data.get("transaction_date"),
                        value_date=data.get("value_date"),
                        action_date=data.get("action_date"),
                        amount=data.get("amount"),
                        description=(data.get("description") or "")[:255],
                    ).first()
                    if existing:
                        for k, v in data.items():
                            setattr(existing, k, v)
                        existing.fallback_key = fallback_key
                        existing.save(update_fields=list(data.keys()) + ["fallback_key"])
                        created = False
                    else:
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

    return {"created": total_created, "updated": total_updated, "accounts": len(accounts_data)}


def run_investec_bank_sync(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    include_pending: bool = False,
    account_filter: Optional[str] = None,
    dry_run: bool = False,
    update_sync_log: bool = True,
) -> dict[str, Any]:
    """
    Sync Investec bank accounts and transactions from the API for ALL configured profiles.
    Returns dict: { created, updated, profiles_synced, errors?, last_synced_at? }
    """
    base_url = getattr(settings, "INVESTEC_BASE_URL", "").strip() or "https://openapi.investec.com"
    profiles = getattr(settings, "INVESTEC_PROFILES", [])

    if not profiles:
        return {
            "created": 0,
            "updated": 0,
            "error": "No Investec credential profiles configured. Check INVESTEC_CLIENT_ID / INVESTEC_CLIENT_SECRET / INVESTEC_API_KEY in settings.",
        }

    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=180)

    total_created = 0
    total_updated = 0
    errors = []

    for i, profile in enumerate(profiles, start=1):
        label = f"Profile {i} ({profile['client_id'][:8]}...)"
        logger.info("Syncing %s", label)
        try:
            res = _sync_single_profile(
                profile=profile,
                base_url=base_url,
                from_date=from_date,
                to_date=to_date,
                include_pending=include_pending,
                account_filter=account_filter,
                dry_run=dry_run,
            )
            total_created += res.get("created", 0)
            total_updated += res.get("updated", 0)
            if res.get("error"):
                errors.append(f"{label}: {res['error']}")
            else:
                logger.info("%s: %d created, %d updated, %d accounts", label, res["created"], res["updated"], res.get("accounts", 0))
        except Exception as e:
            errors.append(f"{label}: {e}")
            logger.exception("Error syncing %s", label)

    result: dict[str, Any] = {
        "created": total_created,
        "updated": total_updated,
        "profiles_synced": len(profiles),
    }
    if errors:
        result["errors"] = errors

    if update_sync_log and not dry_run and not errors:
        now = timezone.now()
        log_obj, _ = InvestecBankSyncLog.objects.get_or_create(key="default", defaults={"last_synced_at": now})
        log_obj.last_synced_at = now
        log_obj.save(update_fields=["last_synced_at"])
        result["last_synced_at"] = now.isoformat()

    return result
