# Generated for PAW drill-through to source transactions

from django.db import migrations


CREATE_VIEW_SQL = """
CREATE OR REPLACE VIEW v_xero_journal_drill AS
SELECT
  o.tenant_id,
  j.account_id,
  a.code AS account_code,
  EXTRACT(YEAR FROM j.date)::int AS year,
  EXTRACT(MONTH FROM j.date)::int AS month,
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
  j.tax_amount,
  COALESCE(t.transaction_source, 'manual_journal') AS transaction_source_type
FROM xero_data_xerojournals j
JOIN xero_core_xerotenant o ON j.organisation_id = o.tenant_id
JOIN xero_metadata_xeroaccount a ON j.account_id = a.account_id AND a.organisation_id = o.tenant_id
LEFT JOIN xero_data_xerotransactionsource t ON j.transaction_source_id = t.transactions_id AND t.organisation_id = o.tenant_id
LEFT JOIN xero_metadata_xerocontacts c ON COALESCE(j.contact_id, t.contact_id) = c.contacts_id AND c.organisation_id = o.tenant_id
LEFT JOIN xero_metadata_xerotracking tk1 ON j.tracking1_id = tk1.id
LEFT JOIN xero_metadata_xerotracking tk2 ON j.tracking2_id = tk2.id;
"""

DROP_VIEW_SQL = """
DROP VIEW IF EXISTS v_xero_journal_drill;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('xero_data', '0005_increase_journal_id_max_length'),
    ]

    operations = [
        migrations.RunSQL(CREATE_VIEW_SQL, DROP_VIEW_SQL),
    ]
