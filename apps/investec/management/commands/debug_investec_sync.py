"""
Debug: compare DB state vs API response for Investec bank transactions.
Run: .venv/bin/python manage.py debug_investec_sync
"""
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.investec.bank_api import (
    fetch_all_transactions,
    get_access_token,
    transaction_to_model_data,
)
from apps.investec.models import InvestecBankAccount, InvestecBankTransaction


class Command(BaseCommand):
    help = "Compare DB vs API for Investec bank transactions (debug)."

    def handle(self, *args, **options):
        # 1) DB state
        self.stdout.write("=== DB STATE ===")
        for acc in InvestecBankAccount.objects.all():
            count = InvestecBankTransaction.objects.filter(account=acc).count()
            self.stdout.write(
                f"  Account id={acc.id} account_id={acc.account_id} account_number={acc.account_number}: {count} transactions"
            )
            if count > 0:
                samples = list(
                    InvestecBankTransaction.objects.filter(account=acc)
                    .order_by("-posting_date", "-posted_order")[:5]
                    .values("id", "uuid", "posting_date", "posted_order", "amount", "description")
                )
                self.stdout.write(f"  Sample rows: {samples}")

        # 2) Call API
        self.stdout.write("\n=== API RESPONSE ===")
        base_url = getattr(settings, "INVESTEC_BASE_URL", "").strip() or "https://openapi.investec.com"
        client_id = getattr(settings, "INVESTEC_CLIENT_ID", "") or ""
        client_secret = getattr(settings, "INVESTEC_CLIENT_SECRET", "") or ""
        api_key = getattr(settings, "INVESTEC_API_KEY", "") or ""
        if not all([client_id, client_secret, api_key]):
            self.stdout.write(self.style.ERROR("Missing credentials, skipping API call"))
            return
        token = get_access_token(base_url, client_id, client_secret, api_key)
        acc = InvestecBankAccount.objects.filter(account_number="363177001").first()
        if not acc:
            acc = InvestecBankAccount.objects.first()
        if not acc:
            self.stdout.write(self.style.ERROR("No InvestecBankAccount in DB"))
            return
        account_id = acc.account_id
        self.stdout.write(f"Fetching transactions for account_id={account_id} (2025-01-01 to 2026-03-09)")
        txns = fetch_all_transactions(
            base_url,
            token,
            account_id,
            from_date=date(2025, 1, 1),
            to_date=date(2026, 3, 9),
        )
        self.stdout.write(f"API returned {len(txns)} transactions")
        for i, t in enumerate(txns[:5]):
            self.stdout.write(f"  API txn[{i}]: keys={list(t.keys())}")
            self.stdout.write(
                f"    postingDate={t.get('postingDate')!r} postedOrder={t.get('postedOrder')!r} uuid={t.get('uuid')!r}"
            )
            self.stdout.write(f"    type={t.get('type')} amount={t.get('amount')} description={t.get('description')!r}")
        if len(txns) > 5:
            self.stdout.write(f"  ... and {len(txns) - 5} more")
        uuids = [t.get("uuid") for t in txns if t.get("uuid")]
        posting_keys = [(t.get("postingDate"), t.get("postedOrder")) for t in txns]
        self.stdout.write(f"  Unique uuids: {len(uuids)} / {len(txns)}")
        self.stdout.write(f"  Unique (postingDate, postedOrder): {len(set(posting_keys))} / {len(txns)}")
        if txns:
            data = transaction_to_model_data(txns[0])
            self.stdout.write(
                f"  First txn mapped: posting_date={data.get('posting_date')!r} posted_order={data.get('posted_order')!r} uuid={data.get('uuid')!r}"
            )
        self.stdout.write("\nDone.")
