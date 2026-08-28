"""Parsing Xero's aged report.

No AgedPayable or AgedReceivable row had ever been written for any tenant,
across every run this system has made. The cause was not the API: the calls
were issued and paid for. Xero returns a ReportWithRows whose top-level Rows
are Section rows, and the SummaryRow lives inside a Section's own Rows — but
the parser only scanned the top level. Every contact parsed as "empty report"
and was skipped, so the answers were bought and thrown away.
"""
from decimal import Decimal

from django.test import TestCase

from apps.xero.xero_data.aged_reports_service import _extract_buckets, _find_summary_row

SUMMARY = {
    'RowType': 'SummaryRow',
    'Cells': [
        {'Value': 'Total'}, {'Value': '10.00'}, {'Value': '20.00'},
        {'Value': '30.00'}, {'Value': '40.00'}, {'Value': '50.00'},
        {'Value': '150.00'},
    ],
}


class AgedReportParseTests(TestCase):
    def test_the_summary_row_is_found_inside_a_section(self):
        """Xero's real shape, and the one the parser used to miss."""
        rows = [{
            'RowType': 'Section',
            'Title': 'Atomic (Pty) Ltd',
            'Rows': [
                {'RowType': 'Row', 'Cells': [{'Value': 'AAINV-107697'}]},
                SUMMARY,
            ],
        }]

        self.assertEqual(_find_summary_row(rows), SUMMARY['Cells'])

    def test_a_top_level_summary_row_still_works(self):
        self.assertEqual(_find_summary_row([SUMMARY]), SUMMARY['Cells'])

    def test_it_descends_through_more_than_one_level(self):
        rows = [{'RowType': 'Section', 'Rows': [{'RowType': 'Section', 'Rows': [SUMMARY]}]}]

        self.assertEqual(_find_summary_row(rows), SUMMARY['Cells'])

    def test_a_report_with_no_summary_row_is_still_reported_as_absent(self):
        rows = [{'RowType': 'Section', 'Rows': [{'RowType': 'Row', 'Cells': []}]}]

        self.assertIsNone(_find_summary_row(rows))

    def test_malformed_rows_do_not_crash_the_parse(self):
        self.assertIsNone(_find_summary_row(None))
        self.assertIsNone(_find_summary_row([None, 'nonsense', 42]))
        self.assertIsNone(_find_summary_row([{'RowType': 'Section', 'Rows': None}]))

    def test_buckets_map_to_the_columns_xero_publishes(self):
        buckets = _extract_buckets(SUMMARY['Cells'])

        self.assertEqual(buckets['current'], Decimal('10.00'))
        self.assertEqual(buckets['one_month'], Decimal('20.00'))
        self.assertEqual(buckets['two_months'], Decimal('30.00'))
        self.assertEqual(buckets['three_months'], Decimal('40.00'))
        self.assertEqual(buckets['older'], Decimal('50.00'))
