"""Binding an Investec account to an entity.

This replaced two dictionaries in code. The behaviour that matters is
unchanged and is pinned here: an account belongs to exactly one entity, an
entity with no binding sees nothing, and the seed migration moved no money
between books.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.investec.models import InvestecEntityAccount
from apps.xero.xero_core.models import XeroTenant


class InvestecEntityAccountTests(TestCase):
    def setUp(self):
        self.a = XeroTenant.objects.create(tenant_id='tenant-a', tenant_name='A Co')
        self.b = XeroTenant.objects.create(tenant_id='tenant-b', tenant_name='B Co')

    def _bind(self, entity, number, kind=InvestecEntityAccount.Kind.BANK, **kwargs):
        return InvestecEntityAccount.objects.create(
            entity=entity, account_number=number, kind=kind, **kwargs
        )

    def test_an_account_cannot_be_claimed_by_two_entities(self):
        # Two claims on one account would put the same money in two sets of books.
        self._bind(self.a, '10012345678')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._bind(self.b, '10012345678')

    def test_a_deactivated_binding_frees_the_account_to_be_rebound(self):
        first = self._bind(self.a, '10012345678')
        first.active = False
        first.save()

        second = self._bind(self.b, '10012345678')

        self.assertEqual(
            InvestecEntityAccount.numbers_for(self.b.pk, InvestecEntityAccount.Kind.BANK),
            ['10012345678'],
        )
        self.assertEqual(second.entity_id, self.b.pk)

    def test_bank_and_share_bindings_do_not_collide(self):
        # The same number could in principle exist on both sides; kind separates them.
        self._bind(self.a, '10082386', InvestecEntityAccount.Kind.BANK)
        self._bind(self.a, '10082386', InvestecEntityAccount.Kind.SHARE)

        self.assertEqual(
            InvestecEntityAccount.numbers_for(self.a.pk, InvestecEntityAccount.Kind.SHARE),
            ['10082386'],
        )

    def test_an_entity_with_no_binding_gets_nothing(self):
        self._bind(self.a, '10012345678')

        self.assertEqual(
            InvestecEntityAccount.numbers_for(self.b.pk, InvestecEntityAccount.Kind.BANK), [],
        )

    def test_an_inactive_binding_is_not_returned(self):
        self._bind(self.a, '10012345678', active=False)

        self.assertEqual(
            InvestecEntityAccount.numbers_for(self.a.pk, InvestecEntityAccount.Kind.BANK), [],
        )
