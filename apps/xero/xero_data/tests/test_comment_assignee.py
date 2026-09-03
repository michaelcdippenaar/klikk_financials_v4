"""
WHO SHOULD ACT on a comment — the fourth axis, and the ways it goes quietly wrong.

The register became a work queue the moment a bookkeeper joined the loop: MC
raises a point against a figure, she answers it, MC closes it. `author_key`
says who WROTE the note, `status` whether it has been WORKED and `decision`
what the verdict is; none of them can say who it is WAITING ON.

Every property below is a way assignment can look fine and not be:

* a name kept as PROSE would rebuild the queue by re-reading text, and break
  the first time someone writes '@anzelle?' or an account is renamed;
* an UNKNOWN name accepted silently is the worst outcome in a review loop —
  the raiser believes the point was passed on and the preparer never sees it,
  and nothing anywhere reports a problem;
* an OMITTED assignee that blanks a stored one empties somebody's queue from a
  client that does not even know the field exists. That is exactly the receipts
  data-loss bug (`tests_archive.py`: "a decision-less PATCH blanked the
  decision"), which is why it is pinned here BEFORE a second writer arrives;
* an assignment the MCP door cannot make means an agent raising a correction
  from the Excel pane lands it in nobody's queue.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from rest_framework.test import APIClient

from apps.xero.xero_data import cube_mentions, pivot_comments

User = get_user_model()

CUBE = '/xero/data/journals/pivot/comments/'
GENERIC = '/xero/data/comments/'


def _payload(**over):
    body = {
        'measure': 'amount',
        'row_dims': ['account_class', 'account'],
        'row_path': ['EXPENSE', '406 - Consulting'],
        'col_dims': ['fin_year'],
        'col_path': 'FY2023',
        'filters': {'tenant': '', 'journal_type': ''},
        'cell_value': 134200,
        'comment': 'Please recode this to 6420.',
    }
    body.update(over)
    return body


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.mc = User.objects.create_user(username='mc', password='pw-not-logged')
        self.anzelle = User.objects.create_user(username='anzelle', password='pw-not-logged')
        # Lazy DDL cached in a module global; TestCase rolls the schema back, so
        # the cache outlives it unless reset per test.
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        self.client.force_authenticate(self.mc)


class AssigneeResolutionTests(_Base):
    def test_a_name_is_stored_resolved_not_as_the_prose_that_produced_it(self):
        r = self.client.post(CUBE, _payload(assignee='anzelle'), format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['assignee_key'], 'anzelle')

    def test_the_at_form_is_input_and_resolves_to_the_same_stored_value(self):
        # '@anzelle' is how a person types it; the queue must not contain two
        # spellings of one bookkeeper.
        r = self.client.post(CUBE, _payload(assignee='@anzelle'), format='json')
        self.assertEqual(r.data['assignee_key'], 'anzelle')

    def test_an_unknown_name_is_a_400_and_writes_nothing(self):
        # THE guard. A misspelling accepted quietly is a point the raiser
        # believes was passed on and the preparer never sees.
        r = self.client.post(CUBE, _payload(assignee='anzelllle'), format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('anzelllle', str(r.data))
        rows = pivot_comments.list_comments({'status': 'all'})
        self.assertEqual(rows, [], 'a rejected assignment still stored the comment')

    def test_me_resolves_to_the_caller(self):
        r = self.client.post(CUBE, _payload(assignee='me'), format='json')
        self.assertEqual(r.data['assignee_key'], 'mc')

    def test_unassigned_is_the_default_and_is_empty_not_null(self):
        r = self.client.post(CUBE, _payload(), format='json')
        self.assertEqual(r.data['assignee_key'], '')


class AssigneeIsNotBlankedByOmissionTests(_Base):
    """The receipts data-loss bug, pinned before a second writer can hit it."""

    def _post(self, **over):
        return self.client.post(CUBE, _payload(**over), format='json')

    def test_a_repost_without_the_field_leaves_the_assignment_intact(self):
        self.assertEqual(self._post(assignee='anzelle').data['assignee_key'], 'anzelle')
        # The add-in re-syncs a sheet and knows nothing about assignment. It
        # must not empty her queue as a side effect of saving the same note.
        again = self._post(comment='Please recode this to 6420. (edited)')
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(again.data['assignee_key'], 'anzelle',
                         'an assignee-less re-post blanked the assignment')

    def test_an_explicit_empty_string_does_unassign(self):
        self._post(assignee='anzelle')
        cleared = self._post(assignee='')
        self.assertEqual(cleared.data['assignee_key'], '',
                         'present-and-empty must be the explicit unassign')


class AssigneeQueueTests(_Base):
    def test_the_filter_returns_only_her_points(self):
        self.client.post(CUBE, _payload(assignee='anzelle'), format='json')
        self.client.post(CUBE, _payload(row_path=['EXPENSE', '407 - Other'],
                                        comment='mine, not hers'), format='json')
        hers = pivot_comments.list_comments({'assignee': 'anzelle'})
        self.assertEqual([r['comment'] for r in hers], ['Please recode this to 6420.'])

    def test_assignment_does_not_disturb_the_other_three_axes(self):
        # author_key = who wrote it, status = worked, decision = verdict.
        # Assignment is a fourth thing and must not borrow any of them.
        r = self.client.post(CUBE, _payload(assignee='anzelle'), format='json')
        self.assertEqual(r.data['author_key'], 'mc')
        self.assertEqual(r.data['status'], 'open')
        self.assertEqual(r.data['decision'], '')


class AssigneeOnTheMcpDoorTests(_Base):
    """An agent raising a correction from the Excel pane uses the generic door."""

    def test_a_non_cube_subject_can_be_assigned_too(self):
        r = self.client.post(GENERIC, {
            'subject_type': 'bank_txn',
            'subject_key': 'inv-uuid-1',
            'subject_label': 'FNB 1234 · 2026-02-11 · R 4,300',
            'comment': 'Recode to 6420 please.',
            'assignee': 'anzelle',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['assignee_key'], 'anzelle')

    def test_an_unknown_name_is_rejected_on_that_door_as_well(self):
        r = self.client.post(GENERIC, {
            'subject_type': 'bank_txn', 'subject_key': 'inv-uuid-2',
            'comment': 'x', 'assignee': 'nobody-by-that-name',
        }, format='json')
        self.assertEqual(r.status_code, 400, r.data)


class BothDoorsStillRequireAuthTests(TestCase):
    """Adding a field must not make a door reachable without a session.

    Pinned here rather than checked once by hand: `assignee` is the first field
    on this table that resolves against the USER table, so an unauthenticated
    caller reaching either door would be able to probe which accounts exist.
    """

    def test_unauthenticated_is_401_on_both_doors(self):
        anon = APIClient()
        for url in (CUBE, GENERIC):
            self.assertEqual(anon.get(url).status_code, 401, url)
            self.assertEqual(anon.post(url, {}, format='json').status_code, 401, url)
