"""The entity's Planning Analytics destination.

TM1ServerConfig and TM1ProcessConfig are global — they describe how to reach
the server, not who may send what to it. Without a per-entity binding,
"submit this entity's close to Planning Analytics" had no defined destination,
which is why the V2 connection reported a flat "unavailable until an
entity-bound destination is approved" for every entity alike.

Two things these tests hold to. Bound is not approved, and not-bound is not the
same problem as not-approved — they need different actions from different
people. And no TM1 connection detail may reach the browser: TM1 is reachable
only from inside the network, and the screen needs to know where a submission
goes, not how to reach it.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.planning_analytics.models import EntityPlanningTarget, TM1ServerConfig
from apps.web_api_v2.models import UserEntityMembership
from apps.web_api_v2.schema import schema
from apps.web_api_v2.services.planning_target import (
    SERVER_SECRET_FIELDS,
    STATE_NOT_APPROVED,
    STATE_NOT_BOUND,
    STATE_READY,
    describe_target,
    entity_can_submit,
)
from apps.web_api_v2.services.source_connections import _planning_analytics_connection
from apps.xero.xero_core.models import XeroTenant

QUERY = """
query T($context: FinancialContextInput!) {
  planningAnalyticsTarget(context: $context) {
    state canSubmit userSafeReason displayName workspace
    defaultScenario defaultVersion approvedAt approvalNote
  }
}
"""


class _Request:
    def __init__(self, user):
        self.user = user
        self.graphql_correlation_id = 'test'


class PlanningTargetTests(TestCase):
    def setUp(self):
        self.tenant = XeroTenant.objects.create(
            tenant_id='tenant-pa', tenant_name='PA Co', fiscal_year_start_month=7,
        )
        self.user = get_user_model().objects.create_user(
            username='pa-viewer', email='pa@example.com', password='irrelevant',
        )
        UserEntityMembership.objects.create(
            user=self.user, entity=self.tenant, role='VIEWER', active=True,
        )
        self.server = TM1ServerConfig.objects.create(
            base_url='https://tm1.internal.invalid:8000',
            username='tm1-service-account',
            password='a-real-looking-secret',
            is_active=True,
        )

    def _bind(self, **overrides):
        defaults = {
            'entity': self.tenant, 'server': self.server,
            'display_name': 'PA Production · Finance',
            'workspace': 'Group Finance / General Ledger',
            'default_scenario': 'Actual', 'default_version': 'Approved close v3',
            'active': True,
        }
        defaults.update(overrides)
        return EntityPlanningTarget.objects.create(**defaults)

    def _approve(self, target):
        target.approved_at = timezone.now()
        target.approved_by = self.user
        target.save()
        return target

    def _execute(self):
        return schema.execute_sync(
            QUERY,
            variable_values={'context': {
                'entityId': self.tenant.tenant_id, 'financialYear': 2026,
                'periodSelection': {'mode': 'ALL', 'months': []},
            }},
            context_value=type('Ctx', (), {'request': _Request(self.user)})(),
        )

    def test_an_entity_with_no_destination_says_nobody_bound_one(self):
        described = describe_target(self.tenant)

        self.assertEqual(described['state'], STATE_NOT_BOUND)
        self.assertIn('nowhere defined to go', described['userSafeReason'])
        self.assertFalse(entity_can_submit(self.tenant))

    def test_a_bound_but_unapproved_destination_is_a_different_problem(self):
        # Different missing action, by a different person, so it must not
        # collapse into "no destination".
        self._bind()
        described = describe_target(self.tenant)

        self.assertEqual(described['state'], STATE_NOT_APPROVED)
        self.assertIn('has not been approved', described['userSafeReason'])
        self.assertNotEqual(described['userSafeReason'], describe_target.__doc__)
        self.assertFalse(entity_can_submit(self.tenant))

    def test_an_approved_destination_is_ready_and_names_itself(self):
        self._approve(self._bind())
        described = describe_target(self.tenant)

        self.assertEqual(described['state'], STATE_READY)
        self.assertIsNone(described['userSafeReason'])
        self.assertEqual(described['displayName'], 'PA Production · Finance')
        self.assertEqual(described['workspace'], 'Group Finance / General Ledger')
        self.assertTrue(entity_can_submit(self.tenant))

    def test_no_tm1_connection_detail_reaches_the_browser(self):
        """TM1 is internal-only. The screen needs where, not how to reach it."""
        self._approve(self._bind())

        payload = self._execute().data['planningAnalyticsTarget']
        serialised = str(payload)

        for field in SERVER_SECRET_FIELDS:
            self.assertNotIn(getattr(self.server, field), serialised)
        self.assertNotIn('tm1.internal.invalid', serialised)
        self.assertNotIn('a-real-looking-secret', serialised)

    def test_the_query_reports_each_state_with_its_own_reason(self):
        self.assertEqual(self._execute().data['planningAnalyticsTarget']['state'], 'NOT_BOUND')

        target = self._bind()
        payload = self._execute().data['planningAnalyticsTarget']
        self.assertEqual(payload['state'], 'NOT_APPROVED')
        self.assertFalse(payload['canSubmit'])

        self._approve(target)
        payload = self._execute().data['planningAnalyticsTarget']
        self.assertEqual(payload['state'], 'READY')
        self.assertTrue(payload['canSubmit'])
        self.assertIsNone(payload['userSafeReason'])

    def test_one_entity_cannot_have_two_active_destinations(self):
        # Two would make "submit this entity" ambiguous, and an ambiguous
        # destination is a wrong one.
        self._bind()
        with self.assertRaises(Exception):
            self._bind(display_name='PA Staging')

    def test_a_destination_for_another_entity_is_not_borrowed(self):
        other = XeroTenant.objects.create(tenant_id='tenant-other', tenant_name='Other Co')
        self._approve(self._bind(entity=other))

        self.assertEqual(describe_target(self.tenant)['state'], STATE_NOT_BOUND)

    def test_the_connection_list_reports_the_binding_rather_than_a_flat_unavailable(self):
        unbound = _planning_analytics_connection(self.tenant)
        self.assertEqual(unbound.availability_code.value, 'NOT_CONFIGURED')
        self.assertIn('nowhere defined to go', unbound.user_safe_reason)

        self._approve(self._bind())
        ready = _planning_analytics_connection(self.tenant)
        self.assertEqual(ready.availability_code.value, 'AVAILABLE')
        self.assertEqual(ready.safe_identity, 'PA Production · Finance')
        self.assertIsNone(ready.user_safe_reason)
