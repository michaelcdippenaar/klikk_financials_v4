"""
Tests for the append-only activity trail.

Three things are load-bearing and each has its own section:

1. ``record_activity`` NEVER raises. Every call site is inside a user-facing
   write; a trail that can fail a receipt archive is worse than no trail.
2. The write paths emit the right action with the right from/to.
3. The endpoints are unreachable by auditors — they are the accounts the
   trail records, and the mount point outside /audit/ IS the access control.

Receipts note: ``whatsapp.klikk_slips`` is absent from the test DB, so the
receipt REVIEW/COMMENT endpoints cannot be hit here (they check the slip
exists first). apps.receipts.tests covers those live, with the raw DDL
fixture; this file exercises the recorder against the receipt views' helper
directly.

Run:  manage.py test apps.activity -v 2
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.activity import models as A
from apps.activity.models import ActivityEvent
from apps.activity.services import bulk_changes, diff, record_activity, record_auditor_read
from apps.audit.models import AuditFinding, AuditFindingComment

User = get_user_model()

LIST_URL = '/api/activity/'
EXPORT_URL = '/api/activity/export/'
ACTORS_URL = '/api/activity/actors/'


def mk_finding(seq=1, **kw):
    return AuditFinding.objects.create(
        fy=2026, ref=f'FY26-{seq:03d}', title=kw.pop('title', 'Payments before bill'),
        severity='HIGH', status='OPEN', category='SUP', source='internal-audit run 13',
        created_by='seed', **kw,
    )


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='reviewer', email='reviewer@example.com', password='pw-irrelevant')
        self.auditor = User.objects.create_user(
            username='auditor@firm.co.za', email='auditor@firm.co.za',
            password='pw-irrelevant', role=User.Role.AUDITOR)
        self.client = self.client_for(self.user)
        self.finding = mk_finding()

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')
        return client

    def actions(self):
        return list(ActivityEvent.objects.order_by('id').values_list('action', flat=True))


# --------------------------------------------------------------------------- #
# 1. The recorder cannot break a write
# --------------------------------------------------------------------------- #
class RecorderRobustnessTests(Base):
    def test_record_activity_swallows_a_dead_database(self):
        request = RequestFactory().post('/whatever/')
        with mock.patch('apps.activity.models.ActivityEvent.objects.create',
                        side_effect=RuntimeError('db gone')):
            self.assertIsNone(record_activity(request, A.FINDING_UPDATED))

    def test_record_activity_survives_a_request_with_no_user(self):
        request = RequestFactory().post('/whatever/')
        event = record_activity(request, A.FINDING_UPDATED, target_kind='finding', target_id=1)
        self.assertIsNotNone(event)
        self.assertEqual(event.actor, '')

    def test_record_activity_accepts_no_request_at_all(self):
        event = record_activity(None, A.FINDING_UPDATED, target_kind='finding', target_id=1)
        self.assertIsNotNone(event)
        self.assertIsNone(event.ip)

    def test_a_dead_trail_does_not_fail_the_write(self):
        """The REAL failure mode: the trail INSERT blows up mid-request.

        Patched at the DB boundary rather than at ``record_activity`` itself —
        the contract is that the recorder absorbs its own failures, so the call
        sites are deliberately unguarded, and a test that patches the recorder
        to raise would be testing a situation that cannot occur.
        """
        with mock.patch('apps.activity.models.ActivityEvent.objects.create',
                        side_effect=RuntimeError('db gone')):
            resp = self.client.post(
                reverse('audit:finding_comments', args=[self.finding.pk]),
                {'text': 'still works'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(AuditFindingComment.objects.count(), 1)
        self.assertFalse(ActivityEvent.objects.exists())

    def test_ip_prefers_the_forwarded_header(self):
        request = RequestFactory().post('/x/', HTTP_X_FORWARDED_FOR='41.1.2.3, 10.0.0.1')
        event = record_activity(request, A.FINDING_UPDATED)
        self.assertEqual(event.ip, '41.1.2.3')

    def test_user_agent_is_capped(self):
        request = RequestFactory().post('/x/', HTTP_USER_AGENT='u' * 900)
        event = record_activity(request, A.FINDING_UPDATED)
        self.assertEqual(len(event.user_agent), 300)

    def test_diff_omits_unchanged_fields(self):
        self.assertEqual(
            diff({'status': 'OPEN', 'owner': 'mc'}, {'status': 'RESOLVED', 'owner': 'mc'},
                 ['status', 'owner']),
            {'status': {'from': 'OPEN', 'to': 'RESOLVED'}},
        )

    def test_bulk_changes_caps_the_id_list(self):
        payload = bulk_changes(range(900), status='RESOLVED')
        self.assertEqual(payload['count'], 900)
        self.assertEqual(len(payload['ids']), 500)
        self.assertTrue(payload['ids_truncated'])
        self.assertEqual(payload['status'], 'RESOLVED')


# --------------------------------------------------------------------------- #
# 2. The write paths
# --------------------------------------------------------------------------- #
class FindingWritePathTests(Base):
    def test_create_is_recorded(self):
        resp = self.client.post(
            reverse('audit:findings'),
            {'title': 'New finding', 'severity': 'HIGH', 'category': 'SUP', 'source': 'manual'},
            format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        event = ActivityEvent.objects.get(action=A.FINDING_CREATED)
        self.assertEqual(event.actor, 'reviewer')
        self.assertEqual(event.target_kind, 'finding')
        self.assertEqual(event.target_id, str(resp.json()['id']))
        self.assertEqual(event.source, 'console')

    def test_status_change_gets_its_own_action_with_from_and_to(self):
        resp = self.client.patch(
            reverse('audit:finding_detail', args=[self.finding.pk]),
            {'status': 'RESOLVED'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        event = ActivityEvent.objects.get(action=A.FINDING_STATUS_CHANGED)
        self.assertEqual(event.changes, {'status': {'from': 'OPEN', 'to': 'RESOLVED'}})
        self.assertIn('FY26-001', event.target_ref)

    def test_each_named_field_gets_its_own_event(self):
        self.client.patch(
            reverse('audit:finding_detail', args=[self.finding.pk]),
            {'status': 'RESOLVED', 'owner': 'MC', 'severity': 'LOW'}, format='json')
        self.assertEqual(
            set(self.actions()),
            {A.FINDING_STATUS_CHANGED, A.FINDING_OWNER_CHANGED, A.FINDING_SEVERITY_CHANGED},
        )

    def test_unnamed_fields_collapse_into_finding_updated(self):
        self.client.patch(
            reverse('audit:finding_detail', args=[self.finding.pk]),
            {'description': 'now with detail'}, format='json')
        event = ActivityEvent.objects.get(action=A.FINDING_UPDATED)
        self.assertEqual(set(event.changes), {'description'})

    def test_a_patch_that_changes_nothing_records_nothing(self):
        self.client.patch(
            reverse('audit:finding_detail', args=[self.finding.pk]),
            {'status': 'OPEN'}, format='json')
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_comment_is_recorded_with_its_reply_flag(self):
        url = reverse('audit:finding_comments', args=[self.finding.pk])
        root = self.client.post(url, {'text': 'top'}, format='json').json()['id']
        self.client.post(url, {'text': 'reply', 'parent_id': root}, format='json')
        events = list(ActivityEvent.objects.filter(action=A.COMMENT_POSTED).order_by('id'))
        self.assertEqual([e.changes['is_reply'] for e in events], [False, True])
        self.assertEqual(events[1].changes['parent_id'], root)
        self.assertEqual(events[0].target_kind, 'comment')

    def test_bulk_is_ONE_event_per_action_not_one_per_finding(self):
        second = mk_finding(2)
        resp = self.client.post(
            reverse('audit:findings_bulk'),
            {'ids': [self.finding.pk, second.pk], 'status': 'RESOLVED', 'comment': 'closed out'},
            format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            sorted(self.actions()), sorted([A.FINDING_BULK_STATUS, A.FINDING_BULK_COMMENT]))
        event = ActivityEvent.objects.get(action=A.FINDING_BULK_STATUS)
        self.assertEqual(event.source, 'bulk')
        self.assertEqual(event.changes['count'], 2)
        self.assertEqual(sorted(event.changes['ids']), sorted([self.finding.pk, second.pk]))

    def test_link_add_and_remove_are_recorded(self):
        # 'asana' is the one link kind resolved inline from the stored label —
        # every other kind queries a table (slips live in whatsapp.klikk_slips,
        # which does not exist in the test DB).
        created = self.client.post(
            reverse('audit:finding_links', args=[self.finding.pk]),
            {'kind': 'asana', 'ref': '1217633700114593', 'label': 'Chase the bill'},
            format='json')
        self.assertEqual(created.status_code, 201, created.content)
        link_id = created.json()['link']['id']
        added = ActivityEvent.objects.get(action=A.LINK_ADDED)
        self.assertEqual(added.changes['kind'], 'asana')
        self.assertEqual(added.changes['ref'], '1217633700114593')

        self.client.delete(reverse('audit:finding_link_delete', args=[link_id]))
        removed = ActivityEvent.objects.get(action=A.LINK_REMOVED)
        # Captured BEFORE the delete, so it still describes what went.
        self.assertEqual(removed.changes['ref'], '1217633700114593')


class AuditorReadTests(Base):
    def test_an_auditor_viewing_finding_detail_is_recorded(self):
        client = self.client_for(self.auditor)
        resp = client.get(reverse('audit:finding_detail', args=[self.finding.pk]))
        self.assertEqual(resp.status_code, 200, resp.content)
        event = ActivityEvent.objects.get(action=A.FINDING_VIEWED)
        self.assertEqual(event.actor, 'auditor@firm.co.za')
        self.assertEqual(event.actor_role, 'auditor')
        self.assertIn('FY26-001', event.target_ref)

    def test_a_standard_user_viewing_the_same_thing_is_NOT_recorded(self):
        resp = self.client.get(reverse('audit:finding_detail', args=[self.finding.pk]))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(ActivityEvent.objects.filter(action=A.FINDING_VIEWED).exists())

    def test_an_anonymous_read_is_not_recorded_as_anyone(self):
        request = RequestFactory().get('/audit/slip/abc/')
        self.assertIsNone(record_auditor_read(request, A.SLIP_VIEWED))
        self.assertFalse(ActivityEvent.objects.exists())


# --------------------------------------------------------------------------- #
# 3. The endpoints
# --------------------------------------------------------------------------- #
class ActivityEndpointTests(Base):
    def seed(self):
        base = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        rows = []
        for i, (actor, action, kind, tid) in enumerate([
            ('mc', A.FINDING_STATUS_CHANGED, 'finding', '1'),
            ('mc', A.COMMENT_POSTED, 'comment', '10'),
            ('anine', A.RECEIPT_ARCHIVED, 'receipt', 'a' * 64),
            ('auditor@firm.co.za', A.FINDING_VIEWED, 'finding', '1'),
        ]):
            e = ActivityEvent.objects.create(
                actor=actor, action=action, target_kind=kind, target_id=tid,
                target_ref=f'ref-{i}', changes={'n': i}, source='console')
            ActivityEvent.objects.filter(pk=e.pk).update(occurred_at=base + dt.timedelta(days=i))
            rows.append(e)
        return rows

    def get(self, **params):
        resp = self.client.get(LIST_URL, params)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        return resp.json()

    def test_auditors_are_403_on_every_activity_endpoint(self):
        client = self.client_for(self.auditor)
        for url in (LIST_URL, EXPORT_URL, ACTORS_URL, '/api/activity/actions/'):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403, url)

    def test_middleware_unit_blocks_the_activity_prefix_for_auditors(self):
        from apps.user.middleware import AuditorGateMiddleware
        gate = AuditorGateMiddleware(lambda r: None)
        self.assertFalse(gate._allowed(RequestFactory().get(LIST_URL)))
        self.assertFalse(gate._allowed(RequestFactory().get(EXPORT_URL)))

    def test_anonymous_is_401(self):
        self.assertEqual(APIClient().get(LIST_URL).status_code, 401)

    def test_newest_first_with_pagination(self):
        self.seed()
        data = self.get(page_size=2)
        self.assertEqual(data['count'], 4)
        self.assertEqual(data['num_pages'], 2)
        self.assertEqual([r['target_ref'] for r in data['results']], ['ref-3', 'ref-2'])
        page2 = self.get(page_size=2, page=2)
        self.assertEqual([r['target_ref'] for r in page2['results']], ['ref-1', 'ref-0'])

    def test_page_size_is_capped(self):
        self.seed()
        self.assertEqual(self.get(page_size=9999)['page_size'], 200)
        self.assertEqual(self.get(page_size='junk')['page_size'], 50)

    def test_filters(self):
        self.seed()
        self.assertEqual(self.get(actor='mc')['count'], 2)
        self.assertEqual(self.get(action=A.COMMENT_POSTED)['count'], 1)
        self.assertEqual(self.get(target_kind='finding')['count'], 2)
        self.assertEqual(self.get(target_kind='finding', target_id='1')['count'], 2)
        self.assertEqual(self.get(q='anine')['count'], 1)
        self.assertEqual(self.get(q='ref-2')['count'], 1)

    def test_date_filters_and_the_inclusive_until(self):
        self.seed()
        self.assertEqual(self.get(since='2026-08-03')['count'], 2)
        # A bare `until` date must INCLUDE that whole day, or a filter for
        # "up to the 3rd" silently drops everything that happened on the 3rd.
        self.assertEqual(self.get(until='2026-08-03')['count'], 3)

    def test_unparseable_dates_are_400_not_a_full_scan(self):
        for params in ({'since': 'yesterday'}, {'until': 'soon'}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(LIST_URL, params).status_code, 400)

    def test_actors_endpoint_lists_distinct_actors(self):
        self.seed()
        self.assertEqual(self.client.get(ACTORS_URL).json()['actors'],
                         ['anine', 'auditor@firm.co.za', 'mc'])

    def test_actions_endpoint_matches_the_apps_own_list(self):
        self.assertEqual(self.client.get('/api/activity/actions/').json()['actions'], list(A.ACTIONS))

    def test_csv_export_shape_and_filters(self):
        self.seed()
        resp = self.client.get(EXPORT_URL, {'actor': 'mc'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')
        self.assertIn('attachment; filename="activity-', resp['Content-Disposition'])
        body = b''.join(resp.streaming_content).decode('utf-8')
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[0][:4], ['occurred_at', 'actor', 'actor_role', 'action'])
        self.assertEqual(len(rows) - 1, 2)  # header + the two 'mc' rows
        self.assertTrue(all(r[1] == 'mc' for r in rows[1:]))

    def test_export_rejects_a_bad_date_too(self):
        self.assertEqual(self.client.get(EXPORT_URL, {'since': 'nope'}).status_code, 400)

    def test_endpoints_are_read_only(self):
        for url in (LIST_URL, EXPORT_URL, ACTORS_URL):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {}, format='json').status_code, 405)
