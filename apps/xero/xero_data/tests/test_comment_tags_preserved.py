"""
ABSENT TAGS MEAN "LEAVE AS IS" — on every door, not just the one that was fixed.

`_norm_tags` returns None for an absent key and says so in its own docstring:
"absent != empty: absent means leave as is". The generic door honours that. The
CUBE door and the BULK door did not: both did `if tags is None: tags = []` and
then wrote `tags = EXCLUDED.tags` unconditionally, so ANY re-post that carried
no `tags` key emptied the stored ones.

That is not a hypothetical. The register is re-posted constantly -- the add-in
re-saves a note whenever the cell is re-saved, and the MCP writes payloads that
have never carried a tag. `tag=audit` is how the year-end audit agent pulls its
own queue out of the register, so a blanked tag is a point that silently drops
out of an agent's work list while still sitting in the table looking fine.

The in-repo precedent is receipts (tests_archive.py: "a decision-less PATCH
blanked the decision"). Same shape, same guard, and it is pinned here per door
because three doors agreeing today is worth nothing if only one of them is
tested.

The distinction each test turns on:

    tags absent      -> stored tags UNTOUCHED
    tags: []         -> stored tags CLEARED (the explicit "remove my tags")
    tags: [...]      -> stored tags REPLACED
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from apps.xero.xero_data import cube_mentions, pivot_comments

User = get_user_model()

CUBE = '/xero/data/journals/pivot/comments/'
BULK = '/xero/data/journals/pivot/comments/bulk/'
GENERIC = '/xero/data/comments/'


def _cell(**over):
    body = {
        'measure': 'amount',
        'row_dims': ['account_class', 'account'],
        'row_path': ['EXPENSE', '406 - Consulting'],
        'col_dims': ['fin_year'],
        'col_path': 'FY2023',
        'filters': {'tenant': '', 'journal_type': ''},
        'cell_value': 134200,
        'comment': 'check this',
    }
    body.update(over)
    return body


def _tags_of(comment_id):
    with connection.cursor() as c:
        c.execute('SELECT tags FROM app.cube_comments WHERE id = %s', [comment_id])
        return list(c.fetchone()[0] or [])


class _Base(TestCase):
    def setUp(self):
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        self.user = User.objects.create_user(username='mc', password='pw-not-logged')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _post(self, url, body):
        r = self.client.post(url, body, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data


class CubeDoorTests(_Base):
    def test_a_repost_without_a_tags_key_does_not_blank_stored_tags(self):
        cid = self._post(CUBE, _cell(tags=['audit', 'fy2026']))['id']
        self.assertEqual(_tags_of(cid), ['audit', 'fy2026'])

        # The add-in re-saving the same cell, with a payload that has never
        # heard of tags. This emptied the queue before the fix.
        self._post(CUBE, _cell(comment='check this again'))
        self.assertEqual(_tags_of(cid), ['audit', 'fy2026'])

    def test_an_explicit_empty_list_still_clears_them(self):
        cid = self._post(CUBE, _cell(tags=['audit']))['id']
        self._post(CUBE, _cell(tags=[]))
        self.assertEqual(_tags_of(cid), [])

    def test_an_explicit_list_still_replaces_them(self):
        cid = self._post(CUBE, _cell(tags=['audit']))['id']
        self._post(CUBE, _cell(tags=['fy2026']))
        self.assertEqual(_tags_of(cid), ['fy2026'])

    def test_a_first_post_with_no_tags_inserts_an_empty_list_not_null(self):
        # The INSERT half: the column is NOT NULL, so absent must still land [].
        cid = self._post(CUBE, _cell())['id']
        self.assertEqual(_tags_of(cid), [])


class BulkDoorTests(_Base):
    def _bulk(self, body):
        r = self.client.post(BULK, body, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_a_bulk_reflag_without_tags_does_not_blank_stored_tags(self):
        cid = self._post(CUBE, _cell(tags=['audit', 'fy2026']))['id']
        # A bulk flag that says nothing about tags -- the MCP-shaped payload.
        self._bulk({'comment': 'check these', 'cells': [_cell()]})
        self.assertEqual(_tags_of(cid), ['audit', 'fy2026'])

    def test_shared_tags_still_apply_across_the_selection(self):
        cid = self._post(CUBE, _cell(tags=['audit']))['id']
        self._bulk({'comment': 'check these', 'tags': ['review'], 'cells': [_cell()]})
        self.assertEqual(_tags_of(cid), ['review'])

    def test_per_cell_tags_still_win_over_shared_tags(self):
        cid = self._post(CUBE, _cell())['id']
        self._bulk({'comment': 'check these', 'tags': ['review'],
                    'cells': [_cell(tags=['fy2026'])]})
        self.assertEqual(_tags_of(cid), ['fy2026'])

    def test_an_explicit_empty_shared_list_still_clears_them(self):
        cid = self._post(CUBE, _cell(tags=['audit']))['id']
        self._bulk({'comment': 'check these', 'tags': [], 'cells': [_cell()]})
        self.assertEqual(_tags_of(cid), [])

    def test_a_new_cell_flagged_with_no_tags_at_all_still_inserts(self):
        # NOT NULL column; this raised NotNullViolation once.
        data = self._bulk({'comment': 'check these', 'cells': [_cell()]})
        self.assertEqual(data['saved'], 1)
        self.assertEqual(_tags_of(data['results'][0]['id']), [])


class GenericDoorTests(_Base):
    """The door that already had the guard — pinned so it cannot regress back
    into agreement with the two that were wrong."""

    def _txn(self, **over):
        body = {'subject_type': 'bank_txn', 'subject_key': 'uuid-1',
                'comment': 'is this ours?'}
        body.update(over)
        return body

    def test_a_repost_without_a_tags_key_does_not_blank_stored_tags(self):
        cid = self._post(GENERIC, self._txn(tags=['audit']))['id']
        self._post(GENERIC, self._txn(comment='still asking'))
        self.assertEqual(_tags_of(cid), ['audit'])

    def test_an_explicit_empty_list_still_clears_them(self):
        cid = self._post(GENERIC, self._txn(tags=['audit']))['id']
        self._post(GENERIC, self._txn(tags=[]))
        self.assertEqual(_tags_of(cid), [])


class AllThreeDoorsAgreeTests(_Base):
    """One property, asserted once per door, so 'they all agree' is a fact."""

    def test_absent_tags_leave_stored_tags_alone_everywhere(self):
        cube_id = self._post(CUBE, _cell(tags=['audit']))['id']
        txn_id = self._post(GENERIC, {'subject_type': 'bank_txn',
                                      'subject_key': 'uuid-2',
                                      'comment': 'is this ours?',
                                      'tags': ['audit']})['id']

        self._post(CUBE, _cell(comment='again'))
        self.client.post(BULK, {'comment': 'again', 'cells': [_cell()]}, format='json')
        self._post(GENERIC, {'subject_type': 'bank_txn', 'subject_key': 'uuid-2',
                             'comment': 'again'})

        self.assertEqual(_tags_of(cube_id), ['audit'])
        self.assertEqual(_tags_of(txn_id), ['audit'])
