from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from graphql import GraphQLError

from apps.web_api_v2.models import UserEntityMembership
from apps.web_api_v2.services.entity_access import require_entity_access
from apps.xero.xero_core.models import XeroTenant


class RequireEntityAccessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='member', password='safe-test-pass',
        )
        self.allowed = XeroTenant.objects.create(
            tenant_id='allowed', tenant_name='Allowed Entity',
        )
        self.other = XeroTenant.objects.create(
            tenant_id='other', tenant_name='Other Entity',
        )
        self.membership = UserEntityMembership.objects.create(
            user=self.user, entity=self.allowed,
        )
        request = SimpleNamespace(user=self.user)
        self.info = SimpleNamespace(context=SimpleNamespace(request=request))

    def test_returns_active_membership(self):
        self.assertEqual(require_entity_access(self.info, self.allowed.pk), self.membership)

    def test_rejects_unassigned_entity_with_stable_code(self):
        with self.assertRaises(GraphQLError) as caught:
            require_entity_access(self.info, self.other.pk)
        self.assertEqual(caught.exception.extensions['code'], 'FORBIDDEN_ENTITY')

    def test_rejects_inactive_membership(self):
        self.membership.active = False
        self.membership.save(update_fields=('active', 'updated_at'))
        with self.assertRaises(GraphQLError):
            require_entity_access(self.info, self.allowed.pk)
