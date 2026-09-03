"""
Tests for the live comment feed, finding-comment threading, and the outbound
comment webhook + its delivery log.

The feed is what makes a comment left by someone else surface without a
reload; the webhook is what tells an outside system, with a row in
CommentWebhookDelivery recording every attempt either way.

Note on receipts: ``whatsapp.klikk_slips`` is an external table absent from the
test DB, so nothing here posts through the receipts VIEW (apps.receipts.tests
does that, with the raw DDL fixture). SlipComment rows are created directly —
the feed reads the model, not the view.

Run:  manage.py test apps.audit.tests.test_comment_feed_and_webhook -v 2
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.comment_webhook import build_payload, notify_comment_created, sign
from apps.audit.models import AuditFinding, AuditFindingComment, CommentWebhookDelivery
from apps.receipts.models import SlipComment

User = get_user_model()

FEED_URL = '/audit/comments/feed/'
HOOK = 'https://hooks.example.com/klikk/comments'
SECRET = 'a-shared-secret'


def mk_finding(seq=1, **kw):
    return AuditFinding.objects.create(
        fy=2026, ref=f'FY26-{seq:03d}', title=kw.pop('title', 'Payments before bill'),
        severity='HIGH', status='OPEN', category='SUP', source='internal-audit run 13',
        created_by='seed', **kw,
    )


class FeedBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='reviewer', email='reviewer@example.com', password='pw-irrelevant')
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')
        self.finding = mk_finding()

    def feed(self, **params):
        resp = self.client.get(FEED_URL, params)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        return resp.json()


class CommentFeedTests(FeedBase):
    def test_shape_and_auth(self):
        self.assertEqual(APIClient().get(FEED_URL).status_code, 401)

        AuditFindingComment.objects.create(
            finding=self.finding, text='please explain', author='mc')
        data = self.feed(since='2020-01-01T00:00:00Z')
        self.assertEqual(set(data), {'now', 'server_time', 'truncated', 'events'})
        self.assertEqual(len(data['events']), 1)
        event = data['events'][0]
        self.assertEqual(event['kind'], 'finding')
        self.assertEqual(event['object_id'], str(self.finding.pk))
        self.assertIn('FY26-001', event['object_ref'])
        self.assertEqual(
            set(event['comment']), {'id', 'parent_id', 'author', 'text', 'created_at'})
        self.assertEqual(event['comment']['author'], 'mc')

    def test_since_is_exclusive_so_nothing_is_delivered_twice(self):
        AuditFindingComment.objects.create(finding=self.finding, text='first', author='mc')
        first = self.feed(since='2020-01-01T00:00:00Z')
        self.assertEqual([e['comment']['text'] for e in first['events']], ['first'])

        # Same cursor the client would send back: no repeats.
        second = self.feed(since=first['now'])
        self.assertEqual(second['events'], [])

        AuditFindingComment.objects.create(finding=self.finding, text='second', author='anine')
        third = self.feed(since=second['now'])
        self.assertEqual([e['comment']['text'] for e in third['events']], ['second'])

    def test_both_surfaces_appear_and_are_ordered_by_time(self):
        AuditFindingComment.objects.create(finding=self.finding, text='on the finding', author='mc')
        SlipComment.objects.create(sha256='a' * 64, text='on the receipt', author='anine')
        data = self.feed(since='2020-01-01T00:00:00Z')
        self.assertEqual(
            [(e['kind'], e['comment']['text']) for e in data['events']],
            [('finding', 'on the finding'), ('receipt', 'on the receipt')],
        )
        # klikk_slips is absent from the test DB — the ref degrades, never raises.
        receipt_event = data['events'][1]
        self.assertEqual(receipt_event['object_id'], 'a' * 64)
        self.assertTrue(receipt_event['object_ref'])

    def test_replies_carry_their_parent_id(self):
        root = AuditFindingComment.objects.create(finding=self.finding, text='q', author='mc')
        AuditFindingComment.objects.create(
            finding=self.finding, parent=root, text='a', author='anine')
        data = self.feed(since='2020-01-01T00:00:00Z')
        self.assertEqual(
            [(e['comment']['text'], e['comment']['parent_id']) for e in data['events']],
            [('q', None), ('a', root.id)],
        )

    def test_cap_is_enforced_and_the_cursor_does_not_skip_the_overflow(self):
        from apps.audit.comment_feed_views import FEED_MAX

        AuditFindingComment.objects.bulk_create([
            AuditFindingComment(finding=self.finding, text=f'c{i}', author='mc')
            for i in range(FEED_MAX + 25)
        ])
        first = self.feed(since='2020-01-01T00:00:00Z')
        self.assertEqual(len(first['events']), FEED_MAX)
        self.assertTrue(first['truncated'])

        # The cursor stops at the LAST DELIVERED event, so the remainder is not lost.
        second = self.feed(since=first['now'])
        seen = [e['comment']['id'] for e in first['events']] + \
               [e['comment']['id'] for e in second['events']]
        self.assertEqual(len(seen), len(set(seen)), 'an event was delivered twice')
        self.assertEqual(len(seen), FEED_MAX + 25, 'events were skipped past the cap')

    def test_bad_since_is_400_not_a_full_scan(self):
        for raw in ('yesterday', '2026-13-45', 'NaN'):
            with self.subTest(raw=raw):
                self.assertEqual(self.client.get(FEED_URL, {'since': raw}).status_code, 400)

    def test_omitted_since_uses_a_short_window_not_the_whole_table(self):
        old = AuditFindingComment.objects.create(finding=self.finding, text='ancient', author='mc')
        AuditFindingComment.objects.filter(pk=old.pk).update(
            created_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
        AuditFindingComment.objects.create(finding=self.finding, text='fresh', author='mc')
        data = self.feed()
        self.assertEqual([e['comment']['text'] for e in data['events']], ['fresh'])

    def test_feed_is_read_only(self):
        self.assertEqual(self.client.post(FEED_URL, {}, format='json').status_code, 405)


class AuditorFeedGateTests(TestCase):
    """The feed sits under /audit/ so auditors can see replies to their questions."""

    def test_middleware_allows_an_auditor_to_read_the_feed(self):
        from django.test import RequestFactory

        from apps.user.middleware import AuditorGateMiddleware
        gate = AuditorGateMiddleware(lambda r: None)
        self.assertTrue(gate._allowed(RequestFactory().get(FEED_URL)))
        # …and still cannot write to it.
        self.assertFalse(gate._allowed(RequestFactory().post(FEED_URL)))


class FindingCommentThreadTests(FeedBase):
    def url(self, pk=None):
        return reverse('audit:finding_comments', args=[pk or self.finding.pk])

    def test_reply_links_to_parent_and_is_reported(self):
        root_id = self.client.post(self.url(), {'text': 'top'}, format='json').json()['id']
        resp = self.client.post(self.url(), {'text': 'reply', 'parent_id': root_id}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['parent_id'], root_id)
        self.assertEqual(AuditFindingComment.objects.get(text='reply').parent_id, root_id)

    def test_reply_to_a_reply_flattens_onto_the_root(self):
        root = self.client.post(self.url(), {'text': 'top'}, format='json').json()['id']
        mid = self.client.post(self.url(), {'text': 'r1', 'parent_id': root}, format='json').json()['id']
        deep = self.client.post(self.url(), {'text': 'r2', 'parent_id': mid}, format='json')
        self.assertEqual(deep.status_code, 201, deep.content)
        self.assertEqual(deep.json()['parent_id'], root)

    def test_parent_from_another_finding_is_400(self):
        other = mk_finding(2)
        foreign = self.client.post(
            self.url(other.pk), {'text': 'elsewhere'}, format='json').json()['id']
        resp = self.client.post(self.url(), {'text': 'reply', 'parent_id': foreign}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(AuditFindingComment.objects.filter(finding=self.finding).exists())

    def test_junk_parent_id_is_400_not_500(self):
        root = self.client.post(self.url(), {'text': 'top'}, format='json').json()['id']
        for raw in ('abc', '', [], {}, 1.5, True, False, root + 9999, -1):
            resp = self.client.post(self.url(), {'text': 'r', 'parent_id': raw}, format='json')
            self.assertEqual(resp.status_code, 400, f'parent_id={raw!r} -> {resp.status_code}')
        self.assertEqual(AuditFindingComment.objects.count(), 1)

    def test_author_comes_from_the_request_never_the_body(self):
        resp = self.client.post(
            self.url(), {'text': 'spoof', 'author': 'someone-else'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['author'], 'reviewer')
        self.assertEqual(AuditFindingComment.objects.get().author, 'reviewer')

    def test_detail_envelope_exposes_parent_id(self):
        root = self.client.post(self.url(), {'text': 'top'}, format='json').json()['id']
        self.client.post(self.url(), {'text': 'reply', 'parent_id': root}, format='json')
        detail = self.client.get(reverse('audit:finding_detail', args=[self.finding.pk])).json()
        self.assertEqual(
            [(c['text'], c['parent_id']) for c in detail['comments']],
            [('top', None), ('reply', root)],
        )

    def test_bulk_comments_stay_top_level(self):
        resp = self.client.post(
            reverse('audit:findings_bulk'),
            {'ids': [self.finding.pk], 'comment': 'bulk note'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(AuditFindingComment.objects.get(text='bulk note').parent_id)


class CommentWebhookTests(FeedBase):
    def url(self):
        return reverse('audit:finding_comments', args=[self.finding.pk])

    def post_comment(self, text='needs an invoice'):
        """POST a comment AND run the on_commit callbacks.

        Delivery is deferred to transaction.on_commit so nothing fires for a
        request that rolls back. TestCase wraps every test in a transaction
        that never commits, so those callbacks must be executed explicitly —
        without this the webhook silently never runs and the tests pass while
        asserting nothing.
        """
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url(), {'text': text}, format='json')

    def test_no_url_configured_means_no_delivery_and_no_log(self):
        with override_settings(COMMENT_WEBHOOK_URL=''):
            with mock.patch('requests.post') as posted:
                self.assertEqual(self.post_comment().status_code, 201)
        posted.assert_not_called()
        self.assertFalse(CommentWebhookDelivery.objects.exists())

    @override_settings(COMMENT_WEBHOOK_URL=HOOK, COMMENT_WEBHOOK_SECRET='')
    def test_success_is_delivered_and_logged(self):
        response = mock.Mock(status_code=200, text='ok')
        with mock.patch('requests.post', return_value=response) as posted:
            created = self.post_comment()
        self.assertEqual(created.status_code, 201, created.content)
        posted.assert_called_once()

        _args, kwargs = posted.call_args
        payload = json.loads(kwargs['data'])
        self.assertEqual(payload['event'], 'comment.created')
        self.assertEqual(payload['kind'], 'finding')
        self.assertEqual(payload['object_id'], str(self.finding.pk))
        self.assertIn('FY26-001', payload['object_ref'])
        self.assertEqual(payload['comment']['author'], 'reviewer')
        self.assertIn(str(self.finding.pk), payload['url'])
        # No secret configured -> no signature header.
        self.assertNotIn('X-Klikk-Signature', kwargs['headers'])

        row = CommentWebhookDelivery.objects.get()
        self.assertEqual(row.comment_kind, 'finding')
        self.assertEqual(row.comment_id, created.json()['id'])
        self.assertEqual(row.author, 'reviewer')
        self.assertEqual(row.status_code, 200)
        self.assertEqual(row.target_url, HOOK)
        self.assertEqual(row.error, '')

    @override_settings(COMMENT_WEBHOOK_URL=HOOK, COMMENT_WEBHOOK_SECRET=SECRET)
    def test_signature_header_is_hmac_sha256_of_the_exact_body(self):
        with mock.patch('requests.post', return_value=mock.Mock(status_code=204, text='')) as posted:
            self.assertEqual(self.post_comment().status_code, 201)
        _args, kwargs = posted.call_args
        body = kwargs['data']
        expected = 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(kwargs['headers']['X-Klikk-Signature'], expected)
        self.assertEqual(sign(body, SECRET), expected)

    @override_settings(COMMENT_WEBHOOK_URL=HOOK)
    def test_a_raising_webhook_still_returns_201_and_logs_the_failure(self):
        with mock.patch('requests.post', side_effect=RuntimeError('connection refused')):
            created = self.post_comment()
        self.assertEqual(created.status_code, 201, created.content)
        # The comment itself is safe.
        self.assertEqual(AuditFindingComment.objects.count(), 1)
        row = CommentWebhookDelivery.objects.get()
        self.assertIsNone(row.status_code)
        self.assertIn('connection refused', row.error)

    @override_settings(COMMENT_WEBHOOK_URL=HOOK)
    def test_a_non_2xx_response_is_logged_with_its_body(self):
        response = mock.Mock(status_code=500, text='x' * 2000)
        with mock.patch('requests.post', return_value=response):
            self.assertEqual(self.post_comment().status_code, 201)
        row = CommentWebhookDelivery.objects.get()
        self.assertEqual(row.status_code, 500)
        self.assertEqual(len(row.response_snippet), CommentWebhookDelivery.SNIPPET_MAX)

    @override_settings(COMMENT_WEBHOOK_URL=HOOK)
    def test_bulk_comment_fans_out_one_delivery_per_comment(self):
        second = mk_finding(2)
        with mock.patch('requests.post', return_value=mock.Mock(status_code=200, text='ok')) as posted:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    reverse('audit:findings_bulk'),
                    {'ids': [self.finding.pk, second.pk], 'comment': 'chase both'},
                    format='json',
                )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(posted.call_count, 2)
        self.assertEqual(CommentWebhookDelivery.objects.count(), 2)

    @override_settings(COMMENT_WEBHOOK_URL=HOOK)
    def test_a_broken_delivery_log_does_not_break_the_comment(self):
        with mock.patch('requests.post', return_value=mock.Mock(status_code=200, text='ok')), \
             mock.patch('apps.audit.models.CommentWebhookDelivery.objects.create',
                        side_effect=RuntimeError('db gone')):
            self.assertEqual(self.post_comment().status_code, 201)
        self.assertEqual(AuditFindingComment.objects.count(), 1)

    @override_settings(COMMENT_WEBHOOK_URL=HOOK)
    def test_notify_never_raises_even_on_a_malformed_target(self):
        comment = AuditFindingComment.objects.create(
            finding=self.finding, text='x', author='mc')
        # object() has no pk / ref — build_payload blows up inside notify, which
        # must swallow it rather than take the caller's write down with it.
        with self.captureOnCommitCallbacks(execute=True):
            notify_comment_created('finding', comment, target=object())
        self.assertEqual(AuditFindingComment.objects.count(), 1)

    def test_receipt_payload_shape(self):
        comment = SlipComment.objects.create(sha256='b' * 64, text='hello', author='mc')
        payload = build_payload('receipt', comment, 'b' * 64)
        self.assertEqual(payload['kind'], 'receipt')
        self.assertEqual(payload['object_id'], 'b' * 64)
        self.assertEqual(payload['comment']['text'], 'hello')
        self.assertIn('b' * 64, payload['url'])
