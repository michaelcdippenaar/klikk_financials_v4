"""
PAGING the register — and the one failure mode that matters more than the feature.

The row shape of this list is a THREE-consumer contract: /audit/cube-comments/
(the auditor view), the Vue console and the Excel add-in all read it. So the
dangerous change here is not a slow page, it is a SILENT TRUNCATION — a default
that returns fewer rows than a consumer expects looks exactly like "there are
only that many", and nothing anywhere reports it. The add-in would sync a
partial register onto a sheet and the sheet would look complete.

Hence the properties below:

* the default window is UNCHANGED (500), so a caller that sends no paging
  parameter receives today exactly what it received yesterday;
* `limit=2000` — what the add-in actually sends — still works and is NOT
  validated against the page-size set, because rejecting it would have been a
  loud break dressed up as a new feature;
* an unknown `page_size` is a 400 that NAMES the allowed set, never a silent
  clamp to the nearest allowed value;
* `count` keeps its original meaning (rows in THIS response) because three
  consumers already read it, and `total` is the new key that makes
  "121 of 121" renderable;
* the total and the page are filtered by the SAME WHERE, or the total describes
  a different population from the rows beside it;
* all three doors answer the same way, since they are meant to be one query.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from apps.xero.xero_data import cube_mentions, pivot_comments
from apps.xero.xero_data.pivot_comments import DEFAULT_PAGE_SIZE, PAGE_SIZES

User = get_user_model()

CUBE = '/xero/data/journals/pivot/comments/'
GENERIC = '/xero/data/comments/'
AUDIT = '/audit/cube-comments/'

DOORS = (CUBE, GENERIC, AUDIT)


_seq = [0]


def _seed(n, *, tag=None, status='open'):
    """n cube comments straight into the table — the doors are not what is
    under test here, and 121 round trips per test is not worth the seconds."""
    rows = []
    with connection.cursor() as c:
        for _ in range(n):
            _seq[0] += 1
            i = _seq[0]
            c.execute(
                'INSERT INTO app.cube_comments '
                '(cell_key, subject_type, subject_key, tenant_id, measure, row_dims, '
                ' row_path, col_dims, col_path, filters, comment, author, author_key, '
                ' status, tags) '
                "VALUES (%s,'cube_cell',%s,'','amount','{}','{}','{}','','{}'::jsonb,"
                " %s,'mc','mc',%s,%s) RETURNING id",
                ['cell-%d' % i, 'cell-%d' % i, 'note %d' % i, status,
                 [tag] if tag else []])
            rows.append(c.fetchone()[0])
    return rows


class _Base(TestCase):
    def setUp(self):
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        self.user = User.objects.create_user(username='mc', password='pw-not-logged')
        self.client = APIClient()
        self.client.force_authenticate(self.user)


class ContractTests(_Base):
    def test_the_allowed_page_sizes_are_exactly_the_five(self):
        self.assertEqual(PAGE_SIZES, (50, 100, 200, 500, 1000))

    def test_the_default_is_the_limit_the_register_already_had(self):
        # Any smaller default would truncate a caller that sends nothing.
        self.assertEqual(DEFAULT_PAGE_SIZE, pivot_comments.DEFAULT_LIMIT)
        self.assertIn(DEFAULT_PAGE_SIZE, PAGE_SIZES)


class NoSilentTruncationTests(_Base):
    def test_a_caller_that_sends_nothing_still_gets_every_row(self):
        # 121 is the live register's size, and the number a footer would say.
        _seed(121)
        for door in DOORS:
            with self.subTest(door=door):
                body = self.client.get(door).data
                self.assertEqual(body['count'], 121)
                self.assertEqual(body['total'], 121)
                self.assertEqual(len(body['results']), 121)
                self.assertFalse(body['has_more'])

    def test_the_add_ins_limit_of_2000_is_still_honoured(self):
        # The pane sends limit=2000. Validating THAT against the page-size set
        # would have 400'd the add-in on every sync.
        _seed(30)
        for door in DOORS:
            with self.subTest(door=door):
                r = self.client.get(door, {'limit': 2000})
                self.assertEqual(r.status_code, 200, r.data)
                self.assertEqual(r.data['count'], 30)

    def test_count_still_means_rows_in_this_response(self):
        _seed(120)
        body = self.client.get(CUBE, {'page_size': 50}).data
        self.assertEqual(body['count'], 50)      # unchanged meaning
        self.assertEqual(body['total'], 120)     # the new key
        self.assertEqual(len(body['results']), 50)


class PageSizeValidationTests(_Base):
    def test_every_allowed_size_is_accepted(self):
        _seed(3)
        for size in PAGE_SIZES:
            for door in DOORS:
                with self.subTest(size=size, door=door):
                    r = self.client.get(door, {'page_size': size})
                    self.assertEqual(r.status_code, 200, r.data)
                    self.assertEqual(r.data['page_size'], size)

    def test_an_unknown_size_is_a_400_naming_the_allowed_set(self):
        _seed(3)
        for bad in (0, 1, 25, 51, 499, 1001, 2000, -50, 'lots', ''):
            if bad == '':
                continue    # empty means "not given"; covered by the default test
            for door in DOORS:
                with self.subTest(bad=bad, door=door):
                    r = self.client.get(door, {'page_size': bad})
                    self.assertEqual(r.status_code, 400, r.data)
                    message = str(r.data.get('error') or r.data.get('detail'))
                    for size in PAGE_SIZES:
                        self.assertIn(str(size), message)

    def test_a_rejected_size_is_never_quietly_clamped(self):
        # The dangerous alternative: 2000 answered with 500 rows reads exactly
        # like "there are only 500", and the caller cannot tell.
        _seed(3)
        r = self.client.get(CUBE, {'page_size': 2000})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('results', r.data)

    def test_a_nonsense_page_number_is_a_400(self):
        _seed(3)
        r = self.client.get(CUBE, {'page': 'two'})
        self.assertEqual(r.status_code, 400, r.data)


class WindowTests(_Base):
    def test_pages_partition_the_register_with_no_repeats_and_no_gaps(self):
        _seed(121)
        seen = []
        for page in (1, 2, 3):
            body = self.client.get(CUBE, {'page_size': 50, 'page': page}).data
            self.assertEqual(body['total'], 121)
            self.assertEqual(body['page'], page)
            seen.extend(r['id'] for r in body['results'])
        self.assertEqual(len(seen), 121)
        self.assertEqual(len(set(seen)), 121, 'a row was served on two pages')

    def test_has_more_is_true_until_the_last_page(self):
        _seed(121)
        self.assertTrue(self.client.get(CUBE, {'page_size': 50, 'page': 1}).data['has_more'])
        self.assertTrue(self.client.get(CUBE, {'page_size': 50, 'page': 2}).data['has_more'])
        self.assertFalse(self.client.get(CUBE, {'page_size': 50, 'page': 3}).data['has_more'])

    def test_a_page_past_the_end_is_empty_and_still_reports_the_total(self):
        _seed(60)
        body = self.client.get(CUBE, {'page_size': 50, 'page': 9}).data
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['total'], 60)
        self.assertFalse(body['has_more'])

    def test_page_without_page_size_uses_the_default(self):
        _seed(3)
        body = self.client.get(CUBE, {'page': 1}).data
        self.assertEqual(body['page_size'], DEFAULT_PAGE_SIZE)


class TotalMatchesTheFilterTests(_Base):
    """A total built from a different WHERE than the page is the exact bug the
    footer exists to prevent — '50 of 300' when the filter matched 60."""

    def test_the_total_respects_a_tag_filter(self):
        _seed(70, tag='audit')
        _seed(30)
        body = self.client.get(CUBE, {'page_size': 50, 'tag': 'audit'}).data
        self.assertEqual(body['total'], 70)
        self.assertEqual(body['count'], 50)

    def test_the_total_respects_the_status_filter(self):
        _seed(40, status='open')
        _seed(25, status='actioned')
        self.assertEqual(self.client.get(CUBE).data['total'], 40)             # default open
        self.assertEqual(self.client.get(CUBE, {'status': 'all'}).data['total'], 65)
        self.assertEqual(self.client.get(CUBE, {'status': 'actioned'}).data['total'], 25)

    def test_the_total_respects_a_free_text_search(self):
        ids = _seed(10)
        with connection.cursor() as c:
            c.execute('SELECT comment FROM app.cube_comments WHERE id = %s', [ids[3]])
            wanted = c.fetchone()[0]
        body = self.client.get(CUBE, {'q': wanted}).data
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['count'], 1)


class DoorsAgreeTests(_Base):
    """Three doors, one query. They drift the moment paging is implemented twice."""

    def test_all_three_doors_report_the_same_total_and_the_same_page(self):
        _seed(121)
        bodies = [self.client.get(door, {'page_size': 100, 'page': 2}).data
                  for door in DOORS]
        totals = {b['total'] for b in bodies}
        counts = {b['count'] for b in bodies}
        ids = {tuple(r['id'] for r in b['results']) for b in bodies}
        self.assertEqual(totals, {121})
        self.assertEqual(counts, {21})
        self.assertEqual(len(ids), 1, 'the doors served different rows')
