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
    beneficiary_to_model_data,
    fetch_accounts,
    fetch_all_transactions,
    fetch_beneficiaries,
    get_access_token,
    transaction_to_model_data,
)
from apps.investec.models import (
    InvestecBankAccount,
    InvestecBankSyncLog,
    InvestecBankTransaction,
    InvestecBeneficiary,
)

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
        owner_map = getattr(settings, "INVESTEC_OWNER_MAP", {})
        for acc in accounts_data:
            obj, _created = InvestecBankAccount.objects.update_or_create(
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
            # Non-destructive owner labelling for personal-expenses grouping:
            # only fill when blank so a hand-corrected owner is never overwritten.
            if owner_map and not obj.owner:
                resolved = (owner_map.get(acc.get("profileId") or "")
                            or owner_map.get(acc.get("accountNumber") or ""))
                if resolved:
                    obj.owner = resolved[:70]
                    obj.save(update_fields=["owner"])

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


def run_investec_beneficiary_sync(dry_run: bool = False) -> dict[str, Any]:
    """
    Sync Investec beneficiaries from the API for ALL configured profiles.

    Upserts on (source_profile, beneficiary_id); rows no longer returned by the API
    are marked is_active=False rather than deleted. Read-only against Investec —
    beneficiaries are created/edited in Investec Online only.

    Returns dict: { created, updated, deactivated, profiles_synced, beneficiaries?, errors? }
    In dry_run the fetched (unsaved) rows are returned under "beneficiaries".
    """
    base_url = getattr(settings, "INVESTEC_BASE_URL", "").strip() or "https://openapi.investec.com"
    profiles = getattr(settings, "INVESTEC_PROFILES", [])

    if not profiles:
        return {
            "created": 0,
            "updated": 0,
            "deactivated": 0,
            "error": "No Investec credential profiles configured. Check INVESTEC_CLIENT_ID / INVESTEC_CLIENT_SECRET / INVESTEC_API_KEY in settings.",
        }

    now = timezone.now()
    total_created = 0
    total_updated = 0
    total_deactivated = 0
    dry_run_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for i, profile in enumerate(profiles, start=1):
        client_id = profile.get("client_id") or ""
        source_profile = f"profile-{i}-{client_id[:8]}"
        label = f"Profile {i} ({client_id[:8]}...)"
        try:
            if not all([client_id, profile.get("client_secret"), profile.get("api_key")]):
                errors.append(f"{label}: incomplete credentials.")
                continue
            token = get_access_token(base_url, client_id, profile["client_secret"], profile["api_key"])
            bens = fetch_beneficiaries(base_url, token)
            logger.info("%s: fetched %d beneficiaries", label, len(bens))

            if dry_run:
                for ben in bens:
                    row = beneficiary_to_model_data(ben)
                    row["source_profile"] = source_profile
                    dry_run_rows.append(row)
                continue

            seen_ben_ids: set[str] = set()
            with transaction.atomic():
                for ben in bens:
                    data = beneficiary_to_model_data(ben)
                    ben_id = data["beneficiary_id"]
                    if not ben_id or ben_id in seen_ben_ids:
                        continue
                    seen_ben_ids.add(ben_id)
                    data["is_active"] = True
                    data["last_seen_at"] = now
                    _obj, created = InvestecBeneficiary.objects.update_or_create(
                        source_profile=source_profile,
                        beneficiary_id=ben_id,
                        defaults=data,
                    )
                    if created:
                        total_created += 1
                    else:
                        total_updated += 1
                # Only deactivate on a non-empty response: an empty list is more
                # likely an API hiccup than a genuinely emptied beneficiary book.
                if seen_ben_ids:
                    total_deactivated += InvestecBeneficiary.objects.filter(
                        source_profile=source_profile,
                        is_active=True,
                    ).exclude(beneficiary_id__in=seen_ben_ids).update(is_active=False)
        except Exception as e:
            errors.append(f"{label}: {e}")
            logger.exception("Error syncing beneficiaries for %s", label)

    result: dict[str, Any] = {
        "created": total_created,
        "updated": total_updated,
        "deactivated": total_deactivated,
        "profiles_synced": len(profiles),
    }
    if dry_run:
        result["beneficiaries"] = dry_run_rows
    if errors:
        result["errors"] = errors
    return result


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
