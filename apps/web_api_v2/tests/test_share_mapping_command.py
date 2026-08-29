"""The V2 share-mapping command.

Mapping decides which holding a transaction belongs to, so this is a change to
financial attribution. The properties that matter:

  - the mapping table is GLOBAL but the authority is not: a caller may only
    attach a name that appears on their own entity's share account;
  - a name already mapped elsewhere is refused, not moved, because moving it
    silently re-attributes transactions already counted against that share;
  - share_code is uniquely constrained, so a name goes in a free slot on the
    existing row, and a full row is refused with a reason;
  - it carries its own capability, separate from running syncs.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.investec.models import (
    InvestecEntityAccount,
    InvestecJseShareNameMapping,
    InvestecJseTransaction,
)
from apps.web_api_v2.models import UserEntityCapability, UserEntityMembership
from apps.xero.xero_core.models import XeroTenant

ACCOUNT = '10082386'


class ShareMappingCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='mapper', email='mapper@example.com', password='irrelevant',
        )
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-map', tenant_name='Map Co', fiscal_year_start_month=7,
        )
        self.membership = UserEntityMembership.objects.create(
            user=self.user, entity=self.tenant, role='VIEWER', active=True,
        )
        self.capability = UserEntityCapability.objects.create(
            membership=self.membership, code='MANAGE_SHARE_MAPPINGS', active=True,
        )
        InvestecEntityAccount.objects.create(
            entity=self.tenant, account_number=ACCOUNT,
            kind=InvestecEntityAccount.Kind.SHARE, active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse(
            'web_api_v2_entities:investec-share-mappings',
            kwargs={'entity_id': self.tenant.tenant_id},
        )

    def _txn(self, share_name):
        return InvestecJseTransaction.objects.create(
            date=date(2025, 8, 10), account_number=ACCOUNT, description='Buy',
            share_name=share_name, type='BE', quantity=Decimal('1'), value=Decimal('-100'),
        )

    def _row(self, name, code, **kwargs):
        return InvestecJseShareNameMapping.objects.create(
            share_name=name, share_code=code, **kwargs
        )

    def _post(self, **body):
        return self.client.post(self.url, body, format='json')

    def test_it_attaches_a_name_to_a_free_slot_on_the_existing_row(self):
        self._txn('We Buy Cars Hlds Ltd')
        self._row('WBC SHORT', 'WBC')

        response = self._post(shareName='We Buy Cars Hlds Ltd', shareCode='WBC')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['changed'])
        self.assertEqual(response.data['slot'], 'share_name2')
        row = InvestecJseShareNameMapping.objects.get(share_code='WBC')
        self.assertEqual(row.share_name2, 'We Buy Cars Hlds Ltd')
        # Attribution changes are not anonymous.
        self.assertEqual(row.mapped_by, self.user)

    def test_a_name_not_on_this_entitys_account_cannot_be_mapped_from_here(self):
        # The table is global; the authority is not.
        self._row('WBC SHORT', 'WBC')

        response = self._post(shareName='Someone Elses Share', shareCode='WBC')

        self.assertEqual(response.status_code, 400)
        self.assertIn('does not appear on this entity', response.data['error']['message'])

    def test_a_name_already_mapped_elsewhere_is_refused_not_moved(self):
        # Moving it would silently re-attribute transactions already counted
        # against the other share.
        self._txn('RCL Foods Limited')
        self._row('RCL', 'RCL', share_name2='RCL Foods Limited')
        self._row('KUMBA', 'KUMBA')

        response = self._post(shareName='RCL Foods Limited', shareCode='KUMBA')

        self.assertEqual(response.status_code, 409)
        self.assertIn('already mapped to RCL', response.data['error']['message'])
        self.assertIsNone(InvestecJseShareNameMapping.objects.get(share_code='KUMBA').share_name2)

    def test_a_row_that_already_holds_three_names_is_refused_with_the_reason(self):
        self._txn('A Fourth Name')
        self._row('One', 'AVI', share_name2='Two', share_name3='Three')

        response = self._post(shareName='A Fourth Name', shareCode='AVI')

        self.assertEqual(response.status_code, 409)
        self.assertIn('three names', response.data['error']['message'])

    def test_an_unknown_share_code_is_refused_rather_than_creating_a_row(self):
        # A new row for a code would be rejected by the unique constraint
        # anyway; saying so is more useful than an integrity error.
        self._txn('Some Share')

        response = self._post(shareName='Some Share', shareCode='NOSUCH')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(InvestecJseShareNameMapping.objects.count(), 0)

    def test_mapping_the_same_name_twice_reports_no_change(self):
        self._txn('AVI')
        self._row('A V I', 'AVI', share_name2='AVI')

        response = self._post(shareName='AVI', shareCode='AVI')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['changed'])
        self.assertIn('already mapped', response.data['reason'])

    def test_the_command_needs_its_own_capability(self):
        # Running a sync and re-attributing a share are different powers.
        self._txn('Some Share')
        self._row('X', 'X')
        UserEntityCapability.objects.filter(pk=self.capability.pk).update(active=False)

        response = self._post(shareName='Some Share', shareCode='X')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['error']['code'], 'CAPABILITY_REQUIRED')

    def test_an_entity_with_no_share_account_has_nothing_it_may_map(self):
        InvestecEntityAccount.objects.filter(entity=self.tenant).update(active=False)
        self._row('X', 'X')

        response = self._post(shareName='Anything', shareCode='X')

        self.assertEqual(response.status_code, 400)
        self.assertIn('No Investec share account is bound', response.data['error']['message'])

    def test_it_lists_the_codes_a_name_may_be_attached_to(self):
        self._row('One', 'AVI')
        self._row('Two', 'SNT')
        self._row('Three', None)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['shareCodes'], ['AVI', 'SNT'])
