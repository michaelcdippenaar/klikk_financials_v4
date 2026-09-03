"""
The BigQuery export is SWITCHED OFF, and that is a decision rather than a bug.

There are no Google credentials on the VM, so every export attempt has failed
for months. CLAUDE.md documents that as known-not-a-regression — a sentence that
has to be re-learned by every reader of the logs, and a state in which a real
regression would be indistinguishable from the expected noise. MC's call was to
disable it explicitly.

The properties that matter, and the ways this goes wrong:

* an export that reaches pandas_gbq anyway when the flag is off — a guard placed
  only at the callers, so the next call site added misses it;
* a skip that RAISES, which reads in the log exactly like the failure the flag
  was meant to remove;
* a skip that says nothing, or says it fifty times, or says it without naming
  the way back on;
* the code DELETED rather than gated, so re-enabling means writing it again;
* trail-balance behaviour changing, which is the one thing in this repo that is
  reconciled against Xero to 0.05.
"""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.xero.xero_integration import services


class FlagTests(SimpleTestCase):
    def test_off_by_default(self):
        from django.conf import settings
        self.assertFalse(settings.BIGQUERY_EXPORT_ENABLED)
        self.assertFalse(services.bigquery_export_enabled())

    @override_settings(BIGQUERY_EXPORT_ENABLED=True)
    def test_on_when_set(self):
        self.assertTrue(services.bigquery_export_enabled())


class SkipTests(SimpleTestCase):
    def test_nothing_reaches_pandas_gbq_while_the_flag_is_off(self):
        # The guard lives in update_google_big_query itself -- the one function
        # every export path (sync, async, batch, and the fallback the async
        # paths drop into) ends up in -- so a call site added later cannot reach
        # BigQuery by forgetting it.
        with patch.object(services, 'pandas_gbq') as gbq, \
                patch.object(services, 'get_google_credentials') as creds:
            services.update_google_big_query(object(), 'Xero.Whatever')
        gbq.to_gbq.assert_not_called()
        creds.assert_not_called()

    def test_the_skip_does_not_raise(self):
        # A raising skip logs like the failure the flag exists to remove, and
        # every caller here treats an exception as "export failed".
        self.assertIsNone(services.update_google_big_query(object(), 'Xero.Whatever'))

    def test_exactly_one_log_line_naming_the_flag_and_the_way_back_on(self):
        with self.assertLogs('apps.xero.xero_integration.services', level='INFO') as logs:
            self.assertTrue(services.skip_bigquery('trail_balance'))
        self.assertEqual(len(logs.output), 1, logs.output)
        line = logs.output[0]
        self.assertIn('DISABLED', line)
        self.assertIn('BIGQUERY_EXPORT_ENABLED', line)
        self.assertIn('trail_balance', line)

    @override_settings(BIGQUERY_EXPORT_ENABLED=True)
    def test_with_the_flag_on_the_export_runs_and_says_nothing_about_being_off(self):
        # The code is GATED, not deleted: flipping the flag reaches pandas_gbq
        # again with no other change.
        with patch.object(services, 'pandas_gbq') as gbq, \
                patch.object(services, 'get_google_credentials', return_value='creds'):
            services.update_google_big_query('df', 'Xero.Whatever')
        gbq.to_gbq.assert_called_once()
        self.assertFalse(services.skip_bigquery('trail_balance'))


class ExportAccountsTests(SimpleTestCase):
    def test_export_accounts_returns_before_touching_the_database(self):
        # It builds three dataframes and concatenates them purely to hand them
        # to an export that is off. The guard is before the first query, so a
        # SimpleTestCase (no DB access allowed) proves it never gets there.
        self.assertIsNone(services.export_accounts('any-tenant'))
