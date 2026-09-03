"""
Tags and @mention notification on app.cube_comments.

Two features, one register. The properties worth pinning are the ones where a
plausible implementation is quietly wrong:

* a tag filter that WIDENS a queue instead of narrowing it (&& vs @>),
* a tag column written straight from client input with no bound,
* a mention regex that fires on an email address quoted in prose,
* a mention that resolves to nobody and is silently dropped,
* an SMTP failure that takes the comment down with it.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.xero.xero_data import cube_mentions, pivot_comments
from apps.xero.xero_data.pivot_comments import _norm_tags

User = get_user_model()

COMMENTS = '/xero/data/journals/pivot/comments/'
PEOPLE = '/xero/data/journals/pivot/people/'
NOTIFY = '/xero/data/journals/pivot/comments/%s/notify/'

LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
# Points at a port nothing listens on, so .send() raises the way the live host
# does today (no EMAIL_* configured -> smtp to localhost:25 -> refused).
BROKEN_SMTP = 'django.core.mail.backends.smtp.EmailBackend'


def _payload(**over):
    body = {
        'measure': 'amount',
        'row_dims': ['account_class', 'account'],
        'row_path': ['EXPENSE', '406 - Consulting'],
        'col_dims': ['fin_year'],
        'col_path': 'FY2023',
        'filters': {'tenant': '', 'journal_type': ''},
        'cell_value': 134200,
        'comment': 'check this',
        'author': 'tester',
    }
    body.update(over)
    return body


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='cube-tester', password='pw-not-logged')
        self.client.force_authenticate(self.user)
        # These modules create their tables lazily and cache "done" in a module
        # global. TestCase rolls each test back, DDL included, so the cache
        # outlives the schema it describes and every test after the first sees
        # "relation app.cube_comments does not exist". Reset both flags and
        # re-create per test.
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()


# ---------------------------------------------------------------- tags ----

class TagNormalisationTests(TestCase):
    def test_lowercases_trims_dedupes_and_drops_empties(self):
        self.assertEqual(_norm_tags(['  Audit ', 'FY2026', 'audit', '', '   ']),
                         ['audit', 'fy2026'])

    def test_leading_hash_is_dropped(self):
        self.assertEqual(_norm_tags(['#audit', '# fy2026']), ['audit', 'fy2026'])

    def test_accepts_a_comma_separated_string(self):
        # The pane and the MCP naturally send different shapes.
        self.assertEqual(_norm_tags('audit, FY2026 ,,'), ['audit', 'fy2026'])

    def test_absent_is_not_the_same_as_empty(self):
        self.assertIsNone(_norm_tags(None))
        self.assertEqual(_norm_tags([]), [])

    def test_count_and_length_are_bounded(self):
        # An unbounded text[] written from a client payload is how one bad
        # add-in build fills the register with junk nobody can clear.
        out = _norm_tags(['tag%d' % i for i in range(500)])
        self.assertEqual(len(out), 20)
        self.assertEqual(len(_norm_tags(['x' * 500])[0]), 40)

    def test_junk_types_do_not_raise(self):
        # Falsy members are dropped rather than stringified: None and 0 are what
        # a half-built client sends, not tags anyone typed.
        self.assertEqual(_norm_tags(42), [])
        self.assertEqual(_norm_tags([None, 0, False, 'ok']), ['ok'])


class TagRoundTripAndFilterTests(_Base):
    def _post(self, tags, row_path, comment='note'):
        return self.client.post(COMMENTS, _payload(
            tags=tags, row_path=row_path, comment=comment), format='json')

    def test_tags_round_trip_normalised(self):
        r = self._post(['  Audit ', '#FY2026', 'audit'], ['EXPENSE', 'a'])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['tags'], ['audit', 'fy2026'])

    def test_single_tag_filter(self):
        self._post(['audit'], ['EXPENSE', 'a'])
        self._post(['vat'], ['EXPENSE', 'b'])
        r = self.client.get(COMMENTS, {'tag': 'audit', 'status': 'all'})
        self.assertEqual([c['tags'] for c in r.data['results']], [['audit']])

    def test_multi_tag_filter_means_ALL_not_any(self):
        """@> not &&. `tags=audit,fy2026` must NARROW to comments carrying both.

        Overlap would return the audit-only row too, quietly widening the very
        queue the caller asked to narrow -- and an agent working tag=audit,fy2026
        would act on last year's comments.
        """
        self._post(['audit'], ['EXPENSE', 'a'])
        self._post(['audit', 'fy2026'], ['EXPENSE', 'b'])
        r = self.client.get(COMMENTS, {'tags': 'audit,fy2026', 'status': 'all'})
        self.assertEqual(r.data['count'], 1)
        self.assertEqual(sorted(r.data['results'][0]['tags']), ['audit', 'fy2026'])

    def test_tag_filter_is_case_insensitive_via_normalisation(self):
        self._post(['Audit'], ['EXPENSE', 'a'])
        r = self.client.get(COMMENTS, {'tag': 'AUDIT', 'status': 'all'})
        self.assertEqual(r.data['count'], 1)

    def test_untagged_comments_still_work(self):
        r = self.client.post(COMMENTS, _payload(), format='json')
        self.assertEqual(r.data['tags'], [])


# ------------------------------------------------------------ mentions ----

class MentionParsingTests(TestCase):
    def test_bare_handle(self):
        self.assertEqual(cube_mentions.parse_mentions('@sarah please look'), ['sarah'])

    def test_email_in_prose_is_NOT_a_mention(self):
        """The false positive that matters most: quoting an address must never
        email that person."""
        self.assertEqual(cube_mentions.parse_mentions('email bob@example.com about it'), [])
        self.assertEqual(cube_mentions.parse_mentions('from accounts@tremly.com yesterday'), [])

    def test_xero_reference_containing_at_is_NOT_a_mention(self):
        self.assertEqual(cube_mentions.parse_mentions('ref INV-2024@KLIKK is wrong'), [])
        self.assertEqual(cube_mentions.parse_mentions('PO123@2024 mismatch'), [])

    def test_trailing_punctuation_is_not_part_of_the_handle(self):
        self.assertEqual(cube_mentions.parse_mentions('ask @sarah. and @jan-smit, ok'),
                         ['sarah', 'jan-smit'])

    def test_explicit_address_forms(self):
        self.assertEqual(cube_mentions.parse_mentions('@sarah@example.com hi'),
                         ['sarah@example.com'])
        self.assertEqual(cube_mentions.parse_mentions('@<a.b+c@example.com> hi'),
                         ['a.b+c@example.com'])

    def test_brackets_and_list_punctuation_may_precede(self):
        self.assertEqual(cube_mentions.parse_mentions('(@bookkeeper) and [@auditor]'),
                         ['bookkeeper', 'auditor'])

    def test_case_insensitive_and_deduplicated(self):
        self.assertEqual(cube_mentions.parse_mentions('@sarah @SARAH @sarah'), ['sarah'])

    def test_bounded(self):
        text = ' '.join('@u%d' % i for i in range(200))
        self.assertEqual(len(cube_mentions.parse_mentions(text)), cube_mentions.MAX_MENTIONS)

    def test_empty_and_none(self):
        self.assertEqual(cube_mentions.parse_mentions(''), [])
        self.assertEqual(cube_mentions.parse_mentions(None), [])


class PeopleDirectoryTests(_Base):
    def test_upsert_and_list(self):
        r = self.client.post(PEOPLE, {'handle': '@Bookkeeper', 'email': 'bk@example.invalid',
                                      'display_name': 'The Bookkeeper'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['handle'], 'bookkeeper')
        r = self.client.get(PEOPLE)
        self.assertEqual(r.data['count'], 1)

    def test_invalid_email_is_refused_not_stored(self):
        """Storing junk here would surface later as a mail failure and look
        like an SMTP problem instead of a data problem."""
        r = self.client.post(PEOPLE, {'handle': 'x', 'email': 'not-an-email'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_invalid_handle_is_refused(self):
        r = self.client.post(PEOPLE, {'handle': 'bad handle!', 'email': 'a@b.com'},
                             format='json')
        self.assertEqual(r.status_code, 400)

    def test_inactive_people_are_hidden_unless_all(self):
        self.client.post(PEOPLE, {'handle': 'old', 'email': 'o@example.invalid',
                                  'active': False}, format='json')
        self.assertEqual(self.client.get(PEOPLE).data['count'], 0)
        self.assertEqual(self.client.get(PEOPLE, {'all': '1'}).data['count'], 1)

    def test_requires_authentication(self):
        anon = APIClient()
        self.assertEqual(anon.get(PEOPLE).status_code, 401)
        self.assertEqual(anon.post(PEOPLE, {}, format='json').status_code, 401)


@override_settings(EMAIL_BACKEND=LOCMEM, DEFAULT_FROM_EMAIL='klikk@example.invalid')
class MentionQueueTests(_Base):
    """Saving a comment QUEUES a mention. It does not send one.

    This class used to assert the opposite, and the change is deliberate. MC
    wrote 68 comments in one day, 33 inside a single hour; per-mention email
    would have put 33 messages into one bookkeeper's inbox that evening. His
    words: "I don't want to spam people." A design that only holds while the
    user keeps their own volume down is the wrong design.

    Never sends to a real person: locmem backend, example.invalid addresses.
    """

    def setUp(self):
        super().setUp()
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid',
                                  'display_name': 'The Bookkeeper'}, format='json')

    def _notify(self, comment_id, **body):
        return self.client.post(NOTIFY % comment_id, body, format='json')

    def test_saving_a_comment_sends_nothing(self):
        from django.core import mail
        mail.outbox = []
        r = self.client.post(COMMENTS, _payload(
            comment='@bookkeeper does this look right?'), format='json')
        self.assertEqual(mail.outbox, [], 'saving a comment sent mail')
        self.assertEqual(r.data['mentions']['queued'], ['bk@example.invalid'])

    def test_the_explicit_trigger_is_what_sends(self):
        from django.core import mail
        mail.outbox = []
        r = self.client.post(COMMENTS, _payload(
            comment='@bookkeeper does this look right?'), format='json')
        sent = self._notify(r.data['id'])
        self.assertEqual(sent.data['notified'], ['bk@example.invalid'])
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        # Still enough to identify the figure without opening Excel.
        for fragment in ('does this look right?', '406 - Consulting', 'FY2023',
                         '134,200.00', 'amount'):
            self.assertIn(fragment, body)

    def test_the_affordance_can_ask_who_is_waiting_before_sending(self):
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper look'), format='json')
        pending = self.client.get(NOTIFY % r.data['id'])
        self.assertEqual(pending.data['count'], 1)
        self.assertEqual(pending.data['pending'][0]['display_name'], 'The Bookkeeper')
        self.assertTrue(pending.data['pending'][0]['sendable'])

    def test_re_saving_does_not_queue_a_second_time(self):
        self.client.post(COMMENTS, _payload(comment='@bookkeeper look'), format='json')
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper look again'),
                             format='json')
        self.assertEqual(r.data['mentions']['already_queued'], ['bk@example.invalid'])
        self.assertEqual(self.client.get(NOTIFY % r.data['id']).data['count'], 1)

    def test_a_delivered_mention_is_never_sent_again(self):
        from django.core import mail
        mail.outbox = []
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper look'), format='json')
        cid = r.data['id']
        self._notify(cid)
        again = self.client.post(COMMENTS, _payload(comment='@bookkeeper look again'),
                                 format='json')
        self.assertEqual(again.data['mentions']['already_notified'], ['bk@example.invalid'])
        self.assertEqual(self._notify(cid).data['notified'], [], 're-triggering re-sent')
        self.assertEqual(len(mail.outbox), 1)

    def test_only_the_named_mentions_are_sent(self):
        from django.core import mail
        self.client.post(PEOPLE, {'handle': 'auditor', 'email': 'aud@example.invalid'},
                         format='json')
        mail.outbox = []
        r = self.client.post(COMMENTS, _payload(
            comment='@bookkeeper and @auditor please'), format='json')
        pending = self.client.get(NOTIFY % r.data['id']).data['pending']
        one = [p['id'] for p in pending if p['handle'] == 'auditor']
        sent = self._notify(r.data['id'], mention_ids=one)
        self.assertEqual(sent.data['notified'], ['aud@example.invalid'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.client.get(NOTIFY % r.data['id']).data['count'], 1)

    def test_a_stood_down_seat_is_QUEUED_and_explained_not_dropped(self):
        # MC's stopgap stood @bookkeeper down so nothing could reach Moore by
        # accident. A mention of it must record the intent and say why it
        # cannot go -- vanishing is the failure the loud rules exist to stop.
        from django.core import mail
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid',
                                  'active': False}, format='json')
        mail.outbox = []
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper urgent'), format='json')
        self.assertEqual(r.data['mentions']['queued'], ['bk@example.invalid'])
        pending = self.client.get(NOTIFY % r.data['id']).data['pending'][0]
        self.assertFalse(pending['sendable'])
        self.assertIn('stood down', pending['blocked_reason'])
        blocked = self._notify(r.data['id'])
        self.assertEqual(blocked.data['notified'], [])
        self.assertEqual(len(blocked.data['blocked']), 1)
        self.assertEqual(mail.outbox, [])

    def test_reactivating_the_seat_makes_what_is_queued_sendable(self):
        # Sendability is read at send time, never frozen onto the row, so
        # bringing a seat back must not require re-typing the comment.
        from django.core import mail
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid',
                                  'active': False}, format='json')
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper urgent'), format='json')
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid',
                                  'active': True}, format='json')
        mail.outbox = []
        self.assertEqual(self._notify(r.data['id']).data['notified'],
                         ['bk@example.invalid'])
        self.assertEqual(len(mail.outbox), 1)

    def test_an_unimplemented_channel_blocks_rather_than_silently_dropping(self):
        # Adding channel='whatsapp' to a row must never start sending WhatsApp:
        # MC's standing rule is no message without his confirmation of
        # recipient AND text, and a seam that no-ops would hide that.
        from django.core import mail
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid',
                                  'channel': 'whatsapp'}, format='json')
        mail.outbox = []
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper hi'), format='json')
        res = self._notify(r.data['id'])
        self.assertEqual(res.data['notified'], [])
        self.assertIn('whatsapp', res.data['blocked'][0]['reason'])
        self.assertEqual(mail.outbox, [])

    def test_unresolved_mention_is_REPORTED_not_dropped(self):
        from django.core import mail
        mail.outbox = []
        r = self.client.post(COMMENTS, _payload(comment='@nobody-here check'), format='json')
        self.assertEqual(r.data['mentions']['unresolved'], ['nobody-here'])
        self.assertEqual(r.data['mentions']['queued'], [])
        self.assertEqual(mail.outbox, [])
        self.assertTrue(r.data['id'], 'the comment itself still saved')

    def test_email_in_prose_queues_nobody(self):
        r = self.client.post(COMMENTS, _payload(
            comment='invoice came from bob@example.com, see ref INV-9@ACME'), format='json')
        self.assertEqual(r.data['mentions']['queued'], [])
        self.assertEqual(r.data['mentions']['unresolved'], [])

    def test_django_user_is_resolvable_when_not_in_the_directory(self):
        User.objects.create_user(username='auditor', email='aud@example.invalid')
        r = self.client.post(COMMENTS, _payload(comment='@auditor please review'),
                             format='json')
        self.assertEqual(r.data['mentions']['queued'], ['aud@example.invalid'])

    def test_directory_wins_over_a_coincidental_django_username(self):
        User.objects.create_user(username='bookkeeper', email='wrong@example.invalid')
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper hi'), format='json')
        self.assertEqual(r.data['mentions']['queued'], ['bk@example.invalid'])


@override_settings(EMAIL_BACKEND=LOCMEM)
class MailFailureIsSafeTests(_Base):
    """A dead mail server must cost an email, never a comment — and now it
    cannot even cost the request, because no send happens inside it."""

    def test_a_dead_backend_cannot_touch_the_comment_at_all(self):
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid'},
                         format='json')
        boom = ConnectionRefusedError('[Errno 111] Connection refused')
        with patch('django.core.mail.EmailMessage.send', side_effect=boom):
            r = self.client.post(COMMENTS, _payload(comment='@bookkeeper urgent'),
                                 format='json')
        # Nothing was even attempted during the save — SMTP is off this path.
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['comment'], '@bookkeeper urgent')
        self.assertEqual(r.data['mentions']['queued'], ['bk@example.invalid'])

    def test_a_failure_at_send_time_is_recorded_and_stays_queued(self):
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid'},
                         format='json')
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper urgent'), format='json')
        with patch('django.core.mail.EmailMessage.send',
                   side_effect=ConnectionRefusedError('[Errno 111] Connection refused')):
            res = self.client.post(NOTIFY % r.data['id'], {}, format='json')
        self.assertEqual(len(res.data['failed']), 1)
        self.assertIn('Connection refused', res.data['failed'][0]['error'])
        with connection.cursor() as c:
            c.execute('SELECT email, notified_at, error FROM app.cube_comment_mentions '
                      'WHERE comment_id = %s', [r.data['id']])
            row = c.fetchone()
        self.assertEqual(row[0], 'bk@example.invalid')
        self.assertIsNone(row[1], 'a failed send was marked delivered')
        self.assertIn('Connection refused', row[2])

    def test_a_failed_mention_can_be_retried_without_re_typing_the_comment(self):
        from django.core import mail
        self.client.post(PEOPLE, {'handle': 'bookkeeper', 'email': 'bk@example.invalid'},
                         format='json')
        r = self.client.post(COMMENTS, _payload(comment='@bookkeeper hi'), format='json')
        with patch('django.core.mail.EmailMessage.send',
                   side_effect=ConnectionRefusedError('down')):
            self.client.post(NOTIFY % r.data['id'], {}, format='json')
        mail.outbox = []
        res = self.client.post(NOTIFY % r.data['id'], {}, format='json')
        self.assertEqual(res.data['notified'], ['bk@example.invalid'])
        self.assertEqual(len(mail.outbox), 1)


class CommentsSurfaceStaysGatedTests(TestCase):
    def test_anonymous_gets_401_on_comments_and_people(self):
        anon = APIClient()
        for path in (COMMENTS, PEOPLE):
            self.assertEqual(anon.get(path).status_code, 401, path)
