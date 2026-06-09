"""Personal-expenses API — function-based DRF views (v4 convention).

Pagination shape mirrors the investec views: ``{count, limit, offset, results}``.
Report shape mirrors ``bank_cost_report_view``: ``{filters, summary, by_*}``.
"""
from decimal import Decimal

from django.db import IntegrityError
from django.db.models import Q
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.investec.models import InvestecBankTransaction
from .models import Category, ClassificationRule, TransactionClassification
from .serializers import (
    CategorySerializer,
    ClassificationRuleSerializer,
    TransactionClassificationSerializer,
)
from .services import classify_transactions


def _money(value):
    return str((value or Decimal('0.00')).quantize(Decimal('0.01')))


def _split_accounts(request):
    out = []
    for raw in request.query_params.getlist('account'):
        out.extend(v.strip() for v in str(raw).split(',') if v.strip())
    return list(dict.fromkeys(out))


def _limit_offset(request):
    try:
        limit = int(request.query_params.get('limit', 100))
    except (TypeError, ValueError):
        limit = 100
    limit = min(max(limit, 1), 1000)
    try:
        offset = int(request.query_params.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    return limit, max(offset, 0)


def _get_classification(txn):
    try:
        return txn.classification
    except TransactionClassification.DoesNotExist:
        return None


def _accumulate(bucket, key, base, amount, is_credit):
    row = bucket.get(key)
    if row is None:
        row = dict(base)
        row.update(transaction_count=0, debit_total=Decimal('0.00'), credit_total=Decimal('0.00'))
        bucket[key] = row
    row['transaction_count'] += 1
    if is_credit:
        row['credit_total'] += amount
    else:
        row['debit_total'] += amount


def _serialize_totals(row):
    net = row['debit_total'] - row['credit_total']
    return {
        **{k: v for k, v in row.items() if k not in ('debit_total', 'credit_total')},
        'debit_total': _money(row['debit_total']),
        'credit_total': _money(row['credit_total']),
        'net': _money(net),
    }


@api_view(['GET'])
def report_view(request):
    """Aggregate classified transactions of a given Category.type (default personal)
    by month, category and account. Params: type, since, until, account."""
    ctype = request.query_params.get('type', Category.TYPE_PERSONAL)
    qs = (TransactionClassification.objects
          .filter(category__type=ctype)
          .select_related('transaction', 'transaction__account', 'category'))

    since = request.query_params.get('since')
    until = request.query_params.get('until')
    if since:
        qs = qs.filter(transaction__transaction_date__gte=since)
    if until:
        qs = qs.filter(transaction__transaction_date__lte=until)
    accounts = _split_accounts(request)
    if accounts:
        qs = qs.filter(
            Q(transaction__account__account_id__in=accounts)
            | Q(transaction__account__account_number__in=accounts)
        )

    by_month, by_category, by_account = {}, {}, {}
    grand = {'transaction_count': 0, 'debit_total': Decimal('0.00'), 'credit_total': Decimal('0.00')}

    for tc in qs.iterator():
        txn = tc.transaction
        account = txn.account
        amount = txn.amount or Decimal('0.00')
        is_credit = txn.type == InvestecBankTransaction.TYPE_CREDIT
        d = txn.transaction_date or txn.posting_date or txn.value_date or txn.action_date
        month_key = d.strftime('%Y-%m') if d else 'undated'
        month_label = d.strftime('%b %Y') if d else 'Undated'

        _accumulate(by_month, month_key, {'month': month_key, 'label': month_label}, amount, is_credit)
        _accumulate(by_category, tc.category.name,
                    {'category': tc.category.name, 'type': tc.category.type}, amount, is_credit)
        _accumulate(by_account, str(txn.account_id), {
            'account_id': txn.account_id,
            'account_number': account.account_number,
            'account_name': account.account_name or account.reference_name or '',
            'owner': account.owner or '',
        }, amount, is_credit)

        grand['transaction_count'] += 1
        if is_credit:
            grand['credit_total'] += amount
        else:
            grand['debit_total'] += amount

    by_month_list = sorted((_serialize_totals(r) for r in by_month.values()), key=lambda x: x['month'])
    by_category_list = sorted((_serialize_totals(r) for r in by_category.values()),
                              key=lambda x: Decimal(x['net']), reverse=True)
    by_account_list = sorted((_serialize_totals(r) for r in by_account.values()),
                             key=lambda x: Decimal(x['net']), reverse=True)

    return Response({
        'filters': {'type': ctype, 'since': since or None, 'until': until or None, 'account': accounts or None},
        'summary': {
            'transaction_count': grand['transaction_count'],
            'debit_total': _money(grand['debit_total']),
            'credit_total': _money(grand['credit_total']),
            'net': _money(grand['debit_total'] - grand['credit_total']),
        },
        'by_month': by_month_list,
        'by_category': by_category_list,
        'by_account': by_account_list,
    })


@api_view(['GET'])
def classified_transaction_list_view(request):
    """List transactions with their classification. ?unclassified=true returns the
    needs-categorising worklist. Params: type, category, is_manual, since, until,
    account, limit, offset."""
    qs = (InvestecBankTransaction.objects
          .select_related('account', 'classification', 'classification__category')
          .all().order_by('-transaction_date', '-posted_order', '-id'))

    if request.query_params.get('unclassified') in ('1', 'true', 'True', 'yes'):
        qs = qs.filter(classification__isnull=True)
    ctype = request.query_params.get('type')
    if ctype:
        qs = qs.filter(classification__category__type=ctype)
    category = request.query_params.get('category')
    if category:
        qs = qs.filter(classification__category_id=int(category)) if category.isdigit() \
            else qs.filter(classification__category__name=category)
    is_manual = request.query_params.get('is_manual')
    if is_manual in ('true', 'True', '1'):
        qs = qs.filter(classification__is_manual=True)
    elif is_manual in ('false', 'False', '0'):
        qs = qs.filter(classification__is_manual=False)
    since = request.query_params.get('since')
    if since:
        qs = qs.filter(transaction_date__gte=since)
    until = request.query_params.get('until')
    if until:
        qs = qs.filter(transaction_date__lte=until)
    accounts = _split_accounts(request)
    if accounts:
        qs = qs.filter(Q(account__account_id__in=accounts) | Q(account__account_number__in=accounts))

    total = qs.count()
    limit, offset = _limit_offset(request)

    results = []
    for txn in qs[offset:offset + limit]:
        tc = _get_classification(txn)
        results.append({
            'id': txn.id,
            'account_number': txn.account.account_number,
            'account_name': txn.account.account_name or txn.account.reference_name or '',
            'owner': txn.account.owner or '',
            'transaction_date': txn.transaction_date.isoformat() if txn.transaction_date else None,
            'type': txn.type,
            'amount': str(txn.amount),
            'description': txn.description,
            'transaction_type': txn.transaction_type,
            'category': ({'id': tc.category_id, 'name': tc.category.name, 'type': tc.category.type} if tc else None),
            'is_manual': tc.is_manual if tc else False,
            'source': tc.source if tc else None,
            'matched_tag': tc.matched_tag if tc else '',
        })
    return Response({'count': total, 'limit': limit, 'offset': offset, 'results': results})


@api_view(['POST', 'PATCH', 'DELETE'])
def override_view(request, transaction_id):
    """Manually set (or clear) the category on one transaction. A manual override
    has is_manual=True and survives auto re-runs."""
    try:
        txn = InvestecBankTransaction.objects.get(id=transaction_id)
    except InvestecBankTransaction.DoesNotExist:
        return Response({'detail': 'Transaction not found.'}, status=404)

    if request.method == 'DELETE':
        TransactionClassification.objects.filter(transaction=txn).delete()
        return Response(status=204)

    category_id = request.data.get('category_id')
    if not category_id:
        return Response({'detail': 'category_id is required.'}, status=400)
    try:
        category = Category.objects.get(id=category_id)
    except (Category.DoesNotExist, ValueError, TypeError):
        return Response({'detail': 'Category not found.'}, status=400)

    tc, _ = TransactionClassification.objects.update_or_create(
        transaction=txn,
        defaults={
            'category': category,
            'is_manual': True,
            'source': TransactionClassification.SOURCE_MANUAL,
            'rule': None,
            'matched_tag': '',
        },
    )
    return Response(TransactionClassificationSerializer(tc).data)


@api_view(['GET', 'POST'])
def rules_list_create_view(request):
    """List or create classification rules (the 'fix the rules' loop)."""
    if request.method == 'POST':
        serializer = ClassificationRuleSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        try:
            serializer.save()
        except IntegrityError:
            return Response({'detail': 'A rule with this tag and transaction_type already exists.'}, status=400)
        return Response(serializer.data, status=201)

    qs = ClassificationRule.objects.select_related('category').all()
    ctype = request.query_params.get('type')
    if ctype:
        qs = qs.filter(category__type=ctype)
    tt = request.query_params.get('transaction_type')
    if tt:
        qs = qs.filter(transaction_type=tt)
    tag = request.query_params.get('tag')
    if tag:
        qs = qs.filter(tag__icontains=tag.upper())
    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category_id=int(category)) if category.isdigit() \
            else qs.filter(category__name=category)

    total = qs.count()
    limit, offset = _limit_offset(request)
    results = ClassificationRuleSerializer(qs[offset:offset + limit], many=True).data
    return Response({'count': total, 'limit': limit, 'offset': offset, 'results': results})


@api_view(['GET', 'PATCH', 'DELETE'])
def rule_detail_view(request, rule_id):
    try:
        rule = ClassificationRule.objects.select_related('category').get(id=rule_id)
    except ClassificationRule.DoesNotExist:
        return Response({'detail': 'Rule not found.'}, status=404)

    if request.method == 'DELETE':
        rule.delete()
        return Response(status=204)
    if request.method == 'PATCH':
        serializer = ClassificationRuleSerializer(rule, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        try:
            serializer.save()
        except IntegrityError:
            return Response({'detail': 'A rule with this tag and transaction_type already exists.'}, status=400)
        return Response(serializer.data)
    return Response(ClassificationRuleSerializer(rule).data)


@api_view(['GET'])
def category_list_view(request):
    qs = Category.objects.all()
    ctype = request.query_params.get('type')
    if ctype:
        qs = qs.filter(type=ctype)
    return Response({'results': CategorySerializer(qs, many=True).data})


@api_view(['POST'])
def classify_trigger_view(request):
    """Re-run the classifier (e.g. after editing rules). Body (all optional):
    {since, until, account, reclassify, dry_run}. Manual overrides are preserved."""
    stats = classify_transactions(
        since=request.data.get('since') or None,
        until=request.data.get('until') or None,
        account=request.data.get('account') or None,
        reclassify=bool(request.data.get('reclassify', False)),
        dry_run=bool(request.data.get('dry_run', False)),
    )
    return Response(stats)
