import os
import tempfile
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.investec.models import InvestecBankAccount, InvestecBankTransaction
from .models import Category, ClassificationRule, TransactionClassification
from .services import classify_transactions


def make_account(owner='MC', account_id='acc-1', number='1001'):
    return InvestecBankAccount.objects.create(
        account_id=account_id, account_number=number, account_name='Test Acc', owner=owner,
    )


def make_txn(account, description, amount='100.00', ttype='CardPurchases',
             type_=InvestecBankTransaction.TYPE_DEBIT, d='2025-01-15'):
    return InvestecBankTransaction.objects.create(
        account=account, type=type_, transaction_type=ttype,
        status=InvestecBankTransaction.STATUS_POSTED, description=description,
        amount=Decimal(amount), transaction_date=date.fromisoformat(d),
    )



def _authed_user():
    """Reads require authentication since the 2026-08-20 lockdown (SECURITY-NOTE.md);
    behaviour tests run as a logged-in user. Anonymous 401s are pinned in
    apps/user/test_auth_lockdown.py."""
    from django.contrib.auth import get_user_model
    user, _ = get_user_model().objects.get_or_create(username='test-authed-caller')
    return user

class ClassifierServiceTests(TestCase):
    def setUp(self):
        self.account = make_account()
        self.groceries = Category.objects.create(type=Category.TYPE_PERSONAL, name='Groceries')
        self.other = Category.objects.create(type=Category.TYPE_PERSONAL, name='Other')

    def test_longest_tag_wins(self):
        ClassificationRule.objects.create(tag='WOOL', category=self.other, transaction_type='CardPurchases')
        ClassificationRule.objects.create(tag='WOOLWORTHS FOOD', category=self.groceries, transaction_type='CardPurchases')
        txn = make_txn(self.account, 'WOOLWORTHS FOOD HALL CT')
        classify_transactions(dry_run=False)
        self.assertEqual(txn.classification.category, self.groceries)

    def test_transaction_type_gating(self):
        ClassificationRule.objects.create(tag='ACME', category=self.groceries, transaction_type='Deposits')
        txn = make_txn(self.account, 'ACME PAYMENT', ttype='CardPurchases')
        classify_transactions(dry_run=False)
        self.assertFalse(TransactionClassification.objects.filter(transaction=txn).exists())

    def test_blank_transaction_type_is_fallback(self):
        ClassificationRule.objects.create(tag='GYM', category=self.other, transaction_type='')
        txn = make_txn(self.account, 'STELLENBOSCH GYM', ttype='CardPurchases')
        classify_transactions(dry_run=False)
        self.assertEqual(txn.classification.category, self.other)

    def test_idempotent_rerun(self):
        ClassificationRule.objects.create(tag='SPAR', category=self.groceries, transaction_type='CardPurchases')
        txn = make_txn(self.account, 'KWIKSPAR DE JONKERS')
        classify_transactions(dry_run=False)
        classify_transactions(dry_run=False)
        self.assertEqual(TransactionClassification.objects.filter(transaction=txn).count(), 1)
        self.assertEqual(txn.classification.category, self.groceries)

    def test_manual_override_survives_rerun(self):
        ClassificationRule.objects.create(tag='SPAR', category=self.groceries, transaction_type='CardPurchases')
        txn = make_txn(self.account, 'SPAR SOMERSET')
        classify_transactions(dry_run=False)
        # promote to manual override on a different category
        tc = txn.classification
        tc.category = self.other
        tc.is_manual = True
        tc.source = TransactionClassification.SOURCE_MANUAL
        tc.save()
        classify_transactions(dry_run=False, reclassify=True)
        tc.refresh_from_db()
        self.assertTrue(tc.is_manual)
        self.assertEqual(tc.category, self.other)

    def test_reclassify_updates_auto_rows(self):
        rule = ClassificationRule.objects.create(tag='SPAR', category=self.groceries, transaction_type='CardPurchases')
        txn = make_txn(self.account, 'SPAR SOMERSET')
        classify_transactions(dry_run=False)
        rule.category = self.other
        rule.save()
        classify_transactions(dry_run=False, reclassify=True)
        txn.classification.refresh_from_db()
        self.assertEqual(txn.classification.category, self.other)

    def test_dry_run_writes_nothing(self):
        ClassificationRule.objects.create(tag='SPAR', category=self.groceries, transaction_type='CardPurchases')
        make_txn(self.account, 'SPAR SOMERSET')
        stats = classify_transactions(dry_run=True)
        self.assertEqual(stats['matched'], 1)
        self.assertEqual(TransactionClassification.objects.count(), 0)

    def test_never_deletes_transactions(self):
        ClassificationRule.objects.create(tag='SPAR', category=self.groceries, transaction_type='CardPurchases')
        make_txn(self.account, 'SPAR SOMERSET')
        make_txn(self.account, 'UNMATCHED VENDOR', d='2025-02-01')
        before = InvestecBankTransaction.objects.count()
        classify_transactions(dry_run=False)
        self.assertEqual(InvestecBankTransaction.objects.count(), before)


class ReportApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(_authed_user())
        self.mc = make_account(owner='MC', account_id='acc-mc', number='1001')
        self.wife = make_account(owner='Wife', account_id='acc-wife', number='2002')
        self.groceries = Category.objects.create(type=Category.TYPE_PERSONAL, name='Groceries')
        self.medical = Category.objects.create(type=Category.TYPE_PERSONAL, name='Medical')
        self.business = Category.objects.create(type=Category.TYPE_BUSINESS, name='Municipal Cost')
        ClassificationRule.objects.create(tag='SPAR', category=self.groceries, transaction_type='CardPurchases')
        ClassificationRule.objects.create(tag='PHARMACY', category=self.medical, transaction_type='CardPurchases')
        ClassificationRule.objects.create(tag='MUNICIPAL', category=self.business, transaction_type='OnlineBankingPayments')
        make_txn(self.mc, 'SPAR A', amount='200.00', d='2025-01-10')
        make_txn(self.wife, 'SPAR B', amount='300.00', d='2025-02-10')
        make_txn(self.mc, 'CLICKS PHARMACY', amount='150.00', d='2025-01-20')
        make_txn(self.mc, 'MUNICIPAL ACCOUNT', amount='999.00', ttype='OnlineBankingPayments', d='2025-01-05')
        classify_transactions(dry_run=False)

    def test_personal_report_excludes_business_and_groups(self):
        resp = self.client.get(reverse('personal_expenses:report'), {'type': 'personal'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # 3 personal txns (200+300+150), business 999 excluded
        self.assertEqual(data['summary']['transaction_count'], 3)
        self.assertEqual(Decimal(data['summary']['net']), Decimal('650.00'))
        cats = {c['category']: Decimal(c['net']) for c in data['by_category']}
        self.assertEqual(cats['Groceries'], Decimal('500.00'))
        self.assertEqual(cats['Medical'], Decimal('150.00'))
        self.assertNotIn('Municipal Cost', cats)
        owners = {a['owner'] for a in data['by_account']}
        self.assertEqual(owners, {'MC', 'Wife'})
        months = [m['month'] for m in data['by_month']]
        self.assertEqual(months, sorted(months))
        self.assertIn('2025-01', months)

    def test_report_account_filter(self):
        resp = self.client.get(reverse('personal_expenses:report'), {'type': 'personal', 'account': '2002'})
        data = resp.json()
        self.assertEqual(data['summary']['transaction_count'], 1)
        self.assertEqual(Decimal(data['summary']['net']), Decimal('300.00'))


class OverrideApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(_authed_user())
        self.account = make_account()
        self.groceries = Category.objects.create(type=Category.TYPE_PERSONAL, name='Groceries')
        self.kids = Category.objects.create(type=Category.TYPE_PERSONAL, name='Kids')
        ClassificationRule.objects.create(tag='SPAR', category=self.groceries, transaction_type='CardPurchases')
        self.txn = make_txn(self.account, 'SPAR SOMERSET')
        classify_transactions(dry_run=False)

    def test_override_sets_manual(self):
        url = reverse('personal_expenses:override', args=[self.txn.id])
        resp = self.client.post(url, {'category_id': self.kids.id}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.txn.classification.refresh_from_db()
        self.assertEqual(self.txn.classification.category, self.kids)
        self.assertTrue(self.txn.classification.is_manual)
        self.assertEqual(self.txn.classification.source, TransactionClassification.SOURCE_MANUAL)

    def test_override_then_classify_keeps_manual(self):
        url = reverse('personal_expenses:override', args=[self.txn.id])
        self.client.post(url, {'category_id': self.kids.id}, format='json')
        classify_transactions(dry_run=False, reclassify=True)
        self.txn.classification.refresh_from_db()
        self.assertEqual(self.txn.classification.category, self.kids)

    def test_override_404_for_unknown_txn(self):
        url = reverse('personal_expenses:override', args=[999999])
        resp = self.client.post(url, {'category_id': self.kids.id}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_delete_override_reverts_to_auto(self):
        url = reverse('personal_expenses:override', args=[self.txn.id])
        self.client.post(url, {'category_id': self.kids.id}, format='json')
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(TransactionClassification.objects.filter(transaction=self.txn).exists())
        classify_transactions(dry_run=False)
        self.assertEqual(self.txn.classification.category, self.groceries)


class RulesApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(_authed_user())
        self.cat = Category.objects.create(type=Category.TYPE_PERSONAL, name='Groceries')
        self.cat2 = Category.objects.create(type=Category.TYPE_PERSONAL, name='Takeaway')

    def test_list_shape(self):
        ClassificationRule.objects.create(tag='SPAR', category=self.cat, transaction_type='CardPurchases')
        resp = self.client.get(reverse('personal_expenses:rules'))
        self.assertEqual(resp.status_code, 200)
        for key in ('count', 'limit', 'offset', 'results'):
            self.assertIn(key, resp.json())

    def test_create_uppercases_tag(self):
        resp = self.client.post(reverse('personal_expenses:rules'),
                                {'tag': 'woolworths', 'category_id': self.cat.id, 'transaction_type': 'CardPurchases'},
                                format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['tag'], 'WOOLWORTHS')

    def test_create_conflict_returns_400(self):
        ClassificationRule.objects.create(tag='SPAR', category=self.cat, transaction_type='CardPurchases')
        resp = self.client.post(reverse('personal_expenses:rules'),
                                {'tag': 'spar', 'category_id': self.cat.id, 'transaction_type': 'CardPurchases'},
                                format='json')
        self.assertEqual(resp.status_code, 400)

    def test_patch_changes_category(self):
        rule = ClassificationRule.objects.create(tag='NANDOS', category=self.cat, transaction_type='CardPurchases')
        resp = self.client.patch(reverse('personal_expenses:rule_detail', args=[rule.id]),
                                 {'category_id': self.cat2.id}, format='json')
        self.assertEqual(resp.status_code, 200)
        rule.refresh_from_db()
        self.assertEqual(rule.category, self.cat2)


class SeedCommandTests(TestCase):
    def _write_csv(self):
        fd, path = tempfile.mkstemp(suffix='.csv')
        with os.fdopen(fd, 'w') as fh:
            fh.write('tag;category;type;transaction_type\n')
            fh.write('spar;Groceries;personal;CardPurchases\n')
            fh.write('KFC;Takeaway;personal;CardPurchases\n')
            fh.write(';;;\n')  # blank tag — skipped
        return path

    def test_seed_is_idempotent_and_uppercases(self):
        path = self._write_csv()
        try:
            call_command('seed_personal_categories', csv=path)
            c1, r1 = Category.objects.count(), ClassificationRule.objects.count()
            call_command('seed_personal_categories', csv=path)
            self.assertEqual(Category.objects.count(), c1)
            self.assertEqual(ClassificationRule.objects.count(), r1)
            self.assertEqual(r1, 2)  # blank-tag row skipped
            self.assertTrue(ClassificationRule.objects.filter(tag='SPAR').exists())
        finally:
            os.remove(path)
