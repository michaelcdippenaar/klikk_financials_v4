"""
Sync Investec Private Bank accounts and transactions from the Investec API into PostgreSQL.

Uses INVESTEC_CLIENT_ID, INVESTEC_CLIENT_SECRET, INVESTEC_API_KEY (and optionally INVESTEC_BASE_URL).
Default date range: last 180 days (API default). Use --from-date / --to-date to override.
"""

import hashlib
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.investec.bank_api import (
    fetch_accounts,
    fetch_all_transactions,
    get_access_token,
    transaction_to_model_data,
)
from apps.investec.models import InvestecBankAccount, InvestecBankTransaction


class Command(BaseCommand):
    help = "Sync Investec Private Bank accounts and transactions from the API into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from-date",
            type=str,
            metavar="YYYY-MM-DD",
            help="Start date for transactions (default: today - 180 days)",
        )
        parser.add_argument(
            "--to-date",
            type=str,
            metavar="YYYY-MM-DD",
            help="End date for transactions (default: today)",
        )
        parser.add_argument(
            "--include-pending",
            action="store_true",
            help="Include pending transactions in the API request",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and log only; do not write to the database",
        )
        parser.add_argument(
            "--account",
            type=str,
            metavar="ID_OR_NUMBER",
            help="Sync only this account: use account_id (e.g. 7050585713810189145359540) or account_number (e.g. 363177001)",
        )

    def handle(self, *args, **options):
        base_url = getattr(settings, "INVESTEC_BASE_URL", "").strip() or "https://openapi.investec.com"
        client_id = getattr(settings, "INVESTEC_CLIENT_ID", "") or ""
        client_secret = getattr(settings, "INVESTEC_CLIENT_SECRET", "") or ""
        api_key = getattr(settings, "INVESTEC_API_KEY", "") or ""

        if not all([client_id, client_secret, api_key]):
            self.stdout.write(
                self.style.ERROR(
                    "Missing Investec API credentials. Set INVESTEC_CLIENT_ID, "
                    "INVESTEC_CLIENT_SECRET, and INVESTEC_API_KEY in settings or .env"
                )
            )
            return

        from_date = None
        to_date = None
        if options.get("from_date"):
            try:
                from_date = date.fromisoformat(options["from_date"])
            except ValueError:
                self.stdout.write(self.style.ERROR(f"Invalid --from-date: {options['from_date']}"))
                return
        if options.get("to_date"):
            try:
                to_date = date.fromisoformat(options["to_date"])
            except ValueError:
                self.stdout.write(self.style.ERROR(f"Invalid --to-date: {options['to_date']}"))
                return
        if to_date is None:
            to_date = date.today()
        if from_date is None:
            from_date = to_date - timedelta(days=180)

        include_pending = options.get("include_pending", False)
        dry_run = options.get("dry_run", False)

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN – no database writes"))

        try:
            self.stdout.write("Obtaining access token...")
            token = get_access_token(base_url, client_id, client_secret, api_key)
            self.stdout.write(self.style.SUCCESS("Token obtained"))

            self.stdout.write("Fetching accounts...")
            accounts_data = fetch_accounts(base_url, token)
            if not accounts_data:
                self.stdout.write(self.style.WARNING("No accounts returned from API"))
                return

            account_filter = (options.get("account") or "").strip()
            if account_filter:
                account_filter_str = str(account_filter)
                accounts_data = [
                    a for a in accounts_data
                    if str(a.get("accountId") or "") == account_filter_str
                    or str(a.get("accountNumber") or "") == account_filter_str
                ]
                if not accounts_data:
                    self.stdout.write(
                        self.style.ERROR(f"No account found with account_id or account_number: {account_filter}")
                    )
                    return
                self.stdout.write(f"Filtering to account: {account_filter}")

            # Dedupe by account_id so we only fetch/save transactions once per account
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
                self.stdout.write(self.style.SUCCESS(f"Upserted {len(accounts_data)} account(s)"))

            total_created = 0
            total_updated = 0
            for acc in accounts_data:
                account_id = acc["accountId"]
                self.stdout.write(f"Fetching transactions for account {account_id} (month-by-month)...")
                txns = fetch_all_transactions(
                    base_url,
                    token,
                    account_id,
                    from_date=from_date,
                    to_date=to_date,
                    include_pending=include_pending,
                )
                self.stdout.write(f"  Got {len(txns)} transaction(s)")

                if dry_run:
                    continue

                bank_account = InvestecBankAccount.objects.get(account_id=account_id)
                with transaction.atomic():
                    # Remove legacy rows that had no uuid and posted_order=0 (all collided into one)
                    deleted = InvestecBankTransaction.objects.filter(
                        account=bank_account,
                        uuid__isnull=True,
                        fallback_key__isnull=True,
                        posted_order__in=(None, 0),
                    ).delete()[0]
                    if deleted:
                        self.stdout.write(f"  Cleared {deleted} legacy placeholder row(s)")
                    for txn in txns:
                        data = transaction_to_model_data(txn)
                        uuid_val = (data.get("uuid") or "").strip() or None
                        posting_date = data.get("posting_date")
                        posted_order = data.get("posted_order")
                        # Coerce to int for lookup; API may return float
                        if posted_order is not None and not isinstance(posted_order, int):
                            try:
                                posted_order = int(posted_order)
                                data["posted_order"] = posted_order
                            except (TypeError, ValueError):
                                posted_order = None

                        # When API returns no uuid and posted_order is 0/None, all rows collide.
                        # Use a stable fallback_key from (transaction_date, value_date, action_date, amount, description).
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
                            # Avoid unique constraint (account, posting_date, posted_order) by clearing posted_order
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
                            obj = InvestecBankTransaction.objects.create(
                                account=bank_account,
                                **data,
                            )
                            created = True

                        if created:
                            total_created += 1
                        else:
                            total_updated += 1

            if not dry_run:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Sync complete: {total_created} created, {total_updated} updated"
                    )
                )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Sync failed: {e}"))
            raise
