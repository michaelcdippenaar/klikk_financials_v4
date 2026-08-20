# Exclude the retired 'journal' mirror from v_xero_journal_drill.
#
# Xero moved the Journals API to the Advanced tier in March 2026, so the
# journal_type='journal' feed is frozen (last entry 2025-11-25) and can no
# longer be refreshed. It duplicates the live transaction / manual_journal /
# system_journal feeds, which are complementary and together form the whole
# ledger. The trial balance already excludes it
# (xero_cube/models.py: journal_type != 'journal'), and the pivot cube now
# excludes it by default, so this view must match or the AI agent's SQL
# answers disagree with every report. Query xero_data_xerojournals directly
# to inspect the legacy mirror.

from django.db import migrations

_SELECT = """
SELECT
  o.tenant_id,
  j.account_id,
  a.code AS account_code,
  EXTRACT(YEAR FROM j.date)::int AS year,
  EXTRACT(MONTH FROM j.date)::int AS month,
  COALESCE(o.fiscal_year_start_month, 7) AS fiscal_year_start_month,
  CASE
    WHEN EXTRACT(MONTH FROM j.date) >= COALESCE(o.fiscal_year_start_month, 7)
    THEN EXTRACT(YEAR FROM j.date)::int
    ELSE EXTRACT(YEAR FROM j.date)::int - 1
  END AS fin_year,
  CASE
    WHEN EXTRACT(MONTH FROM j.date) >= COALESCE(o.fiscal_year_start_month, 7)
    THEN EXTRACT(MONTH FROM j.date)::int - COALESCE(o.fiscal_year_start_month, 7) + 1
    ELSE EXTRACT(MONTH FROM j.date)::int + (12 - COALESCE(o.fiscal_year_start_month, 7)) + 1
  END AS fin_period,
  COALESCE(j.contact_id, t.contact_id) AS contact_id,
  c.name AS contact_name,
  j.tracking1_id,
  tk1.option AS tracking1_option,
  j.tracking2_id,
  tk2.option AS tracking2_option,
  j.id,
  j.journal_id,
  j.journal_number,
  j.journal_type,
  j.date,
  j.description,
  j.reference,
  j.amount,
  j.debit,
  j.credit,
  j.tax_amount,
  COALESCE(t.transaction_source, 'manual_journal') AS transaction_source_type
FROM xero_data_xerojournals j
JOIN xero_core_xerotenant o ON j.organisation_id = o.tenant_id
JOIN xero_metadata_xeroaccount a ON j.account_id = a.account_id AND a.organisation_id = o.tenant_id
LEFT JOIN xero_data_xerotransactionsource t ON j.transaction_source_id = t.transactions_id AND t.organisation_id = o.tenant_id
LEFT JOIN xero_metadata_xerocontacts c ON COALESCE(j.contact_id, t.contact_id) = c.contacts_id AND c.organisation_id = o.tenant_id
LEFT JOIN xero_metadata_xerotracking tk1 ON j.tracking1_id = tk1.id
LEFT JOIN xero_metadata_xerotracking tk2 ON j.tracking2_id = tk2.id
"""

FORWARD_SQL = (
    "DROP VIEW IF EXISTS v_xero_journal_drill;\n"
    "CREATE VIEW v_xero_journal_drill AS"
    + _SELECT
    + "WHERE j.journal_type <> 'journal';\n"
)

REVERSE_SQL = (
    "DROP VIEW IF EXISTS v_xero_journal_drill;\n"
    "CREATE VIEW v_xero_journal_drill AS"
    + _SELECT
    + ";\n"
)


class Migration(migrations.Migration):

    dependencies = [
        ('xero_data', '0015_transactionsource_has_attachments'),
    ]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
