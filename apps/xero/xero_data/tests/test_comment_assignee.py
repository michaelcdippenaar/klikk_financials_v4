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
from django.db import connection
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


def _log():
    """The assignment log, oldest first: (comment_id, from, to, held_by, email, by)."""
    with connection.cursor() as c:
        c.execute('SELECT comment_id, from_role, to_role, held_by, held_by_email, '
                  'changed_by FROM app.cube_comment_assignments ORDER BY id')
        return c.fetchall()


def _person(handle, display, email, active=True):
    """A directory row. Written here, never inferred -- the same rule the
    mentions module states: an address is entered on purpose."""
    with connection.cursor() as c:
        c.execute('INSERT INTO app.cube_people (handle, display_name, email, active) '
                  'VALUES (%s,%s,%s,%s) ON CONFLICT (handle) DO UPDATE SET '
                  'display_name = EXCLUDED.display_name, email = EXCLUDED.email, '
                  'active = EXCLUDED.active', [handle, display, email, active])


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.mc = User.objects.create_user(
            username='mc', email='mc@tremly.com', password='pw-not-logged')
        # Lazy DDL cached in a module global; TestCase rolls the schema back, so
        # the cache outlives it unless reset per test.
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        # ROLES, not people: 'bookkeeper' is what a comment stores, and who
        # holds it is a property of the directory.
        _person('bookkeeper', 'Anzelle', 'anzelle@example.com')
        _person('mc', 'MC', 'mc@tremly.com')
        self.client.force_authenticate(self.mc)


class AssigneeResolutionTests(_Base):
    def test_the_handle_is_stored_not_the_person_behind_it(self):
        # The whole point of MC's design: replacing a bookkeeper must not
        # require rewriting the comments she was sent.
        r = self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['assignee_role'], 'bookkeeper')
        self.assertNotIn('anzelle', r.data['assignee_role'])

    def test_the_at_form_and_case_resolve_to_the_one_stored_handle(self):
        r = self.client.post(CUBE, _payload(assignee='@Bookkeeper'), format='json')
        self.assertEqual(r.data['assignee_role'], 'bookkeeper')

    def test_an_unknown_handle_is_a_400_and_writes_nothing(self):
        # THE guard. A typo'd role routes work to a role nobody holds, and
        # nothing anywhere reports it.
        r = self.client.post(CUBE, _payload(assignee='bookeeper'), format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('bookeeper', str(r.data))
        rows = pivot_comments.list_comments({'status': 'all'})
        self.assertEqual(rows, [], 'a rejected assignment still stored the comment')

    def test_an_inactive_handle_is_refused_for_a_new_assignment(self):
        _person('bookkeeper', 'Anzelle', 'anzelle@example.com', active=False)
        r = self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self.assertEqual(r.status_code, 400, r.data)

    def test_me_resolves_through_the_directory_by_address_not_by_username(self):
        r = self.client.post(CUBE, _payload(assignee='me'), format='json')
        self.assertEqual(r.data['assignee_role'], 'mc')

    def test_me_fails_loudly_when_the_caller_is_not_in_the_directory(self):
        stranger = User.objects.create_user(username='x', email='x@example.com', password='p')
        self.client.force_authenticate(stranger)
        r = self.client.post(CUBE, _payload(assignee='me'), format='json')
        self.assertEqual(r.status_code, 400, r.data)

    def test_unassigned_is_the_default_and_is_empty_not_null(self):
        r = self.client.post(CUBE, _payload(), format='json')
        self.assertEqual(r.data['assignee_role'], '')


class AssigneeIsNotBlankedByOmissionTests(_Base):
    """The receipts data-loss bug, pinned before a second writer can hit it."""

    def _post(self, **over):
        return self.client.post(CUBE, _payload(**over), format='json')

    def test_a_repost_without_the_field_leaves_the_assignment_intact(self):
        self.assertEqual(self._post(assignee='bookkeeper').data['assignee_role'], 'bookkeeper')
        # The add-in re-syncs a sheet and knows nothing about assignment. It
        # must not empty her queue as a side effect of saving the same note.
        again = self._post(comment='Please recode this to 6420. (edited)')
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(again.data['assignee_role'], 'bookkeeper',
                         'an assignee-less re-post blanked the assignment')

    def test_an_explicit_empty_string_does_unassign(self):
        self._post(assignee='bookkeeper')
        cleared = self._post(assignee='')
        self.assertEqual(cleared.data['assignee_role'], '',
                         'present-and-empty must be the explicit unassign')


class AssigneeQueueTests(_Base):
    def test_the_filter_returns_only_her_points(self):
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self.client.post(CUBE, _payload(row_path=['EXPENSE', '407 - Other'],
                                        comment='mine, not hers'), format='json')
        hers = pivot_comments.list_comments({'assignee': 'bookkeeper'})
        self.assertEqual([r['comment'] for r in hers], ['Please recode this to 6420.'])

    def test_assignment_does_not_disturb_the_other_three_axes(self):
        # author_key = who wrote it, status = worked, decision = verdict.
        # Assignment is a fourth thing and must not borrow any of them.
        r = self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
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
            'assignee': 'bookkeeper',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['assignee_role'], 'bookkeeper')

    def test_an_unknown_name_is_rejected_on_that_door_as_well(self):
        r = self.client.post(GENERIC, {
            'subject_type': 'bank_txn', 'subject_key': 'inv-uuid-2',
            'comment': 'x', 'assignee': 'no-such-role',
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


class RoleIndirectionTests(_Base):
    """The property MC's design exists for, and the two ways it bites back."""

    def test_replacing_the_person_touches_no_comment(self):
        r = self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        before = r.data['updated_at']
        # Anzelle leaves; Thabo takes the role. One directory row.
        _person('bookkeeper', 'Thabo', 'thabo@example.com')
        rows = pivot_comments.list_comments({'assignee': 'bookkeeper'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['assignee_role'], 'bookkeeper')
        self.assertEqual(rows[0]['updated_at'], before,
                         'changing who holds a role rewrote the comment')

    def test_deactivating_a_role_does_not_orphan_its_open_points(self):
        # Refusing NEW assignment to a stood-down role is right; hiding the
        # points already sitting in it is how work disappears silently.
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        _person('bookkeeper', 'Anzelle', 'anzelle@example.com', active=False)
        rows = pivot_comments.list_comments({'assignee': 'bookkeeper'})
        self.assertEqual(len(rows), 1, 'a stood-down role swallowed its open points')
        self.assertEqual(rows[0]['assignee_role'], 'bookkeeper')


class AssignmentIsRecordedInTheTrailTests(_Base):
    """A handle cannot preserve who held it; the append-only trail can.

    Without this the register answers "who was this sent to in 2026?" with
    whoever holds the role in 2027 — confidently, and wrongly.
    """

    def _events(self):
        from apps.activity.models import ActivityEvent
        return list(ActivityEvent.objects.filter(action='cube_comment.assigned'))

    def test_the_holder_at_the_time_is_recorded_beside_the_handle(self):
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        events = self._events()
        self.assertEqual(len(events), 1, 'assignment left no trace')
        self.assertEqual(events[0].changes['assignee_role'],
                         {'from': '', 'to': 'bookkeeper'})
        self.assertEqual(events[0].actor, 'mc')
        # The holder snapshot lives in the log, not duplicated on the event.
        self.assertEqual(_log()[0][2:5], ('bookkeeper', 'Anzelle', 'anzelle@example.com'))

    def test_the_record_survives_the_person_being_replaced(self):
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        _person('bookkeeper', 'Thabo', 'thabo@example.com')
        self.assertEqual(_log()[0][3], 'Anzelle',
                         'the log was rewritten by a directory change')

    def test_a_repost_that_says_nothing_about_assignment_records_nothing(self):
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self.client.post(CUBE, _payload(comment='same point, retyped'), format='json')
        self.assertEqual(len(self._events()), 1, 'a plain re-save logged a phantom assignment')


class AssignmentLogTests(_Base):
    """Where a point has been, and since when — the number that says a loop stalled.

    The comment row knows only where a point is NOW. Ageing ("with the
    bookkeeper 12 days") and handover ("who was this actually with") both need
    the moves, and neither can be backfilled after the fact.
    """

    def test_the_first_assignment_is_logged_not_only_subsequent_moves(self):
        # The commonest case by far is raised once and never moved. If only
        # CHANGES were logged, that case would have no row and no ageing.
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        log = _log()
        self.assertEqual(len(log), 1, 'the initial assignment left no row')
        self.assertEqual(log[0][1:3], ('', 'bookkeeper'))
        self.assertEqual(log[0][5], 'mc')

    def test_a_move_appends_rather_than_replacing(self):
        _person('reviewer', 'MC', 'mc@tremly.com')
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self.client.post(CUBE, _payload(assignee='reviewer'), format='json')
        self.assertEqual([(r[1], r[2]) for r in _log()],
                         [('', 'bookkeeper'), ('bookkeeper', 'reviewer')])

    def test_reassigning_to_the_same_seat_is_not_a_change_of_hands(self):
        # Otherwise retyping a note resets the ageing clock, and the number
        # that is supposed to reveal a stall quietly stops revealing it.
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self.client.post(CUBE, _payload(comment='retyped', assignee='bookkeeper'), format='json')
        self.assertEqual(len(_log()), 1)

    def test_unassigning_is_logged_too(self):
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self.client.post(CUBE, _payload(assignee=''), format='json')
        self.assertEqual([(r[1], r[2]) for r in _log()],
                         [('', 'bookkeeper'), ('bookkeeper', '')])

    def test_a_post_that_never_mentions_assignment_logs_nothing(self):
        self.client.post(CUBE, _payload(), format='json')
        self.assertEqual(_log(), [])


class RenameMigrationTests(_Base):
    """The path production actually takes, which a fresh table never exercises.

    Every test above builds the table from scratch, so the rename guard never
    fires in them — it is exactly the shape of change that passes its tests and
    then does nothing on the one database that mattered. This puts the deployed
    state back and runs the DDL over it.
    """

    def _columns(self):
        with connection.cursor() as c:
            c.execute("SELECT column_name FROM information_schema.columns "
                      "WHERE table_schema = 'app' AND table_name = 'cube_comments' "
                      "AND column_name IN ('assignee_key', 'assignee_role')")
            return sorted(r[0] for r in c.fetchall())

    def _rewind_to_deployed_shape(self):
        with connection.cursor() as c:
            c.execute('ALTER TABLE app.cube_comments RENAME COLUMN assignee_role TO assignee_key')
        pivot_comments._ready = False

    def test_a_deployed_assignee_key_column_is_renamed_not_duplicated(self):
        self._rewind_to_deployed_shape()
        self.assertEqual(self._columns(), ['assignee_key'])
        pivot_comments._ensure_table()
        self.assertEqual(self._columns(), ['assignee_role'],
                         'the rename left both columns, or neither')

    def test_the_rename_carries_any_value_across(self):
        # Zero rows hold one today, which is why the window is open — but the
        # guard must not be the reason we find that out the hard way.
        self.client.post(CUBE, _payload(assignee='bookkeeper'), format='json')
        self._rewind_to_deployed_shape()
        pivot_comments._ensure_table()
        rows = pivot_comments.list_comments({'assignee': 'bookkeeper'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['assignee_role'], 'bookkeeper')

    def test_running_the_ddl_twice_is_a_no_op(self):
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        self.assertEqual(self._columns(), ['assignee_role'])
