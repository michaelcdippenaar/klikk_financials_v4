"""Classification engine for personal-expenses.

Ports the validated matcher (longest-tag substring, gated by Investec
transaction_type) onto the v4 InvestecBankTransaction model. Pure-ish:
`classify_transactions` is the single entry point used by both the management
command and the re-run API endpoint.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Q

from apps.investec.models import InvestecBankTransaction
from .models import Category, ClassificationRule, TransactionClassification


def load_active_rules():
    """Return ``dict[transaction_type] -> [(TAG, category_id, rule_id, priority), ...]``.

    The ``''`` bucket holds rules that apply to any transaction_type. Each
    bucket is sorted longest-tag-first (priority as secondary key) so the first
    substring hit is the most specific.
    """
    by_tt = defaultdict(list)
    rows = ClassificationRule.objects.filter(is_active=True).values_list(
        'tag', 'transaction_type', 'category_id', 'id', 'priority'
    )
    for tag, tt, category_id, rule_id, priority in rows:
        by_tt[(tt or '').strip()].append(((tag or '').upper(), category_id, rule_id, priority))
    for tt in by_tt:
        by_tt[tt].sort(key=lambda r: (len(r[0]), r[3]), reverse=True)
    return by_tt


def match_transaction(description, transaction_type, rules_by_tt):
    """Return ``(tag, category_id, rule_id)`` for the best rule, or ``None``.

    Candidates = rules gated to this transaction_type + the 'any' bucket. The
    longest tag that is a substring of the upper-cased description wins;
    ``priority`` breaks ties.
    """
    d = (description or '').upper()
    tt = (transaction_type or '').strip()
    candidates = rules_by_tt.get(tt, []) + rules_by_tt.get('', [])
    best = None  # (tag, category_id, rule_id, priority)
    for tag, category_id, rule_id, priority in candidates:
        if tag and tag in d:
            if best is None or (len(tag), priority) > (len(best[0]), best[3]):
                best = (tag, category_id, rule_id, priority)
    if best is None:
        return None
    return best[0], best[1], best[2]


def classify_transactions(*, since=None, until=None, account=None, dry_run=True, reclassify=False):
    """Classify InvestecBankTransaction rows against the active rules.

    Guarantees:
      * manual overrides (``is_manual=True``) are NEVER reconsidered;
      * without ``reclassify`` only transactions lacking any classification are touched;
      * only ADDS/UPDATES ``TransactionClassification`` rows — never deletes transactions;
      * ``dry_run=True`` computes stats and writes nothing.

    Returns a JSON-friendly stats dict.
    """
    rules_by_tt = load_active_rules()

    qs = InvestecBankTransaction.objects.all()
    if since:
        qs = qs.filter(transaction_date__gte=since)
    if until:
        qs = qs.filter(transaction_date__lte=until)
    if account:
        accounts = [a.strip() for a in str(account).split(',') if a.strip()]
        if accounts:
            qs = qs.filter(Q(account__account_id__in=accounts) | Q(account__account_number__in=accounts))

    qs = qs.exclude(classification__is_manual=True)
    if not reclassify:
        qs = qs.filter(classification__isnull=True)

    cat_type = dict(Category.objects.values_list('id', 'type'))
    cat_name = dict(Category.objects.values_list('id', 'name'))

    matched = 0
    unmatched = 0
    by_type = defaultdict(lambda: {'count': 0, 'amount': Decimal('0.00')})
    by_category = defaultdict(lambda: {'count': 0, 'amount': Decimal('0.00')})
    unmatched_samples = defaultdict(int)
    pending = []  # (txn_id, category_id, rule_id, matched_tag)

    for txn in qs.iterator():
        match = match_transaction(txn.description, txn.transaction_type, rules_by_tt)
        if match is None:
            unmatched += 1
            unmatched_samples[' '.join((txn.description or '').upper().split())[:26]] += 1
            continue
        tag, category_id, rule_id = match
        matched += 1
        amount = txn.amount or Decimal('0.00')
        t = cat_type.get(category_id, '')
        by_type[t]['count'] += 1
        by_type[t]['amount'] += amount
        cname = cat_name.get(category_id, str(category_id))
        by_category[cname]['count'] += 1
        by_category[cname]['amount'] += amount
        pending.append((txn.id, category_id, rule_id, tag))

    if not dry_run and pending:
        with db_transaction.atomic():
            for txn_id, category_id, rule_id, tag in pending:
                TransactionClassification.objects.update_or_create(
                    transaction_id=txn_id,
                    defaults={
                        'category_id': category_id,
                        'rule_id': rule_id,
                        'matched_tag': tag,
                        'source': TransactionClassification.SOURCE_AUTO,
                        'is_manual': False,
                    },
                )

    return {
        'dry_run': dry_run,
        'reclassify': reclassify,
        'matched': matched,
        'unmatched': unmatched,
        'written': 0 if dry_run else len(pending),
        'by_type': {k: {'count': v['count'], 'amount': str(v['amount'])} for k, v in by_type.items()},
        'by_category': {
            k: {'count': v['count'], 'amount': str(v['amount'])}
            for k, v in sorted(by_category.items(), key=lambda kv: -kv[1]['amount'])
        },
        'unmatched_samples': dict(sorted(unmatched_samples.items(), key=lambda kv: -kv[1])[:25]),
    }
