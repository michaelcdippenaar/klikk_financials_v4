# Fix v_cash_actuals_weekly: tighten the debt_service INSTAL token.
#
# DEFECT (found by the Report 1 verification pass, 2026-06-25):
# The debt_service DEBIT rule in 0026 used a bare 'INSTAL' token, intended to
# catch instalment / budget-instalment payments. It OVER-MATCHED equipment
# *installation* descriptions ("WIFI INSTALATION", "GDK INSTALLATIONS",
# "ALARM INSTALASIE", "CAMERA INSTALAT...") — 12 live Klikk rows totalling
# R145,048.77 — and bucketed genuine OPERATING outflows as debt_service.
#
# apps/investec/cashflow_categories.py (the canonical classifier the view is
# documented to stay in sync with) uses 'INSTAL?MENT', which correctly matches
# only INSTALMENT / INSTALLMENT and never INSTALATION/INSTALLATION. The view was
# the side that diverged; this migration re-syncs it to the classifier intent by
# replacing the bare 'INSTAL' with 'INSTALMENT|INSTALLMENT'.
#
# Verified against live data: zero genuine INSTALMENT/INSTALLMENT *description*
# rows exist, so this tightening drops nothing legitimate; real budget
# instalments are independently caught by the BudgetInstalments transaction_type
# rule (21 live rows), which is unchanged.
#
# 0026 is ALREADY APPLIED in the live DB, so its SQL is NOT edited in place
# (an applied migration is immutable history). This migration supersedes the
# view definition via CREATE OR REPLACE VIEW — a metadata-only operation: the
# column list and order are unchanged, so no rebuild and no lock on the
# underlying ~20k-row transaction table. Zero-downtime: trivial.

from django.db import migrations


CREATE_VIEW_SQL = r"""
CREATE OR REPLACE VIEW v_cash_actuals_weekly AS
WITH attr AS (
    -- INVESTEC_OWNER_MAP inlined: account_number -> entity / capacity / liquid
    SELECT * FROM (VALUES
        ('10011910139',   'MC personal',        'personal', TRUE),
        ('1100588588501', 'MC personal',        'personal', TRUE),
        ('1100588588540', 'MC personal',        'personal', TRUE),
        ('10014792677',   'L Dippenaar',        'personal', TRUE),
        ('1100116634500', 'L Dippenaar',        'personal', TRUE),
        ('10011924075',   'Klikk',              'business', TRUE),
        ('363177001',     'Klikk',              'business', FALSE),
        ('363177003',     'Klikk',              'business', FALSE),
        ('10013017883',   'MLD Trust',          'business', TRUE),
        ('1100591058540', 'MLD Trust',          'business', TRUE),
        ('10013648134',   'Testamentary Trust', 'business', TRUE),
        ('1100603381540', 'Testamentary Trust', 'business', TRUE),
        ('10014572054',   'Lucanaude',          'business', TRUE),
        ('1100612949540', 'Lucanaude',          'business', TRUE)
    ) AS a(account_number, entity, capacity, liquid)
),
txn AS (
    SELECT
        t.id,
        t.account_id,
        acc.account_number,
        attr.entity,
        attr.capacity,
        attr.liquid,
        t.type AS direction,                       -- 'CREDIT' (in) / 'DEBIT' (out)
        COALESCE(t.transaction_date, t.posting_date) AS txn_date,
        date_trunc('week', COALESCE(t.transaction_date, t.posting_date))::date AS week_start_date,
        t.amount,
        t.running_balance,
        t.posted_order,
        upper(COALESCE(t.description, '')) AS udesc,
        t.transaction_type
    FROM investec_investecbanktransaction t
    JOIN investec_investecbankaccount acc ON acc.id = t.account_id
    JOIN attr ON attr.account_number = acc.account_number
    WHERE COALESCE(t.transaction_date, t.posting_date) IS NOT NULL
),
categorised AS (
    SELECT
        txn.*,
        CASE
            -- 1. internal transfer (any of the 14 account numbers in the desc) — eliminates
            WHEN udesc ~ '(10011910139|1100588588501|1100588588540|10014792677|1100116634500|10011924075|363177001|363177003|10013017883|1100591058540|10013648134|1100603381540|10014572054|1100612949540)'
                THEN 'transfer_internal'
            -- 2. CREDIT keyword rules
            WHEN direction = 'CREDIT' AND udesc ~ 'PERESEC|DIVIDEND|DIV PMT|LEGAEPERE' THEN 'jse_dividend'
            WHEN direction = 'CREDIT' AND udesc ~ 'ANCHOR|BCI CORE|WESTBROOKE|FLEXIBLE INCOME' THEN 'anchor_income'
            WHEN direction = 'CREDIT' AND udesc ~ 'RENT|HUUR|LEASE|TENANT|BOERDERY|RENTAL' THEN 'rental_income'
            -- 3. DEBIT keyword rules
            WHEN direction = 'DEBIT' AND udesc ~ 'VAT201|VALUE ADDED|(^|[^A-Z])VAT([^A-Z]|$)' THEN 'vat'
            WHEN direction = 'DEBIT' AND udesc ~ 'FLEXIBOND|MORTGAGE|BOND REPAYMENT|LOAN REPAYMENT|FACILITY|INSTALMENT|INSTALLMENT|FINANCE CHARGE|INTEREST CHARGED' THEN 'debt_service'
            WHEN direction = 'DEBIT' AND udesc ~ 'TREMLY|MLD TRUST|MANAGEMENT FEE|MGMT FEE|LUCANAUDE|LUCA NAUDE' THEN 'related_party'
            -- 4. transaction_type default (debits)
            WHEN direction = 'DEBIT' AND transaction_type = 'CardPurchases' THEN 'operating_outflow'
            WHEN direction = 'DEBIT' AND transaction_type = 'ATMWithdrawals' THEN 'personal_drawing'
            WHEN direction = 'DEBIT' AND transaction_type = 'FeesAndInterest' THEN 'debt_service'
            WHEN direction = 'DEBIT' AND transaction_type = 'DebitOrders' THEN 'operating_outflow'
            WHEN direction = 'DEBIT' AND transaction_type = 'BudgetInstalments' THEN 'debt_service'
            -- 5. capacity-aware fallback
            WHEN direction = 'DEBIT' AND capacity = 'personal' THEN 'personal_drawing'
            WHEN direction = 'DEBIT' THEN 'operating_outflow'
            ELSE 'other'
        END AS category
    FROM txn
),
-- Flow aggregation: summed amount per (week, entity, capacity, direction).
flows AS (
    SELECT
        week_start_date,
        entity,
        capacity,
        direction,
        SUM(amount) AS amount_total,
        SUM(amount) FILTER (WHERE category NOT IN ('transfer_internal', 'related_party')) AS amount_external,
        COUNT(*) AS txn_count
    FROM categorised
    GROUP BY week_start_date, entity, capacity, direction
),
-- Week-end closing balance per account = the LAST running_balance in the week
-- (ordered by date then posted_order). Liquid accounts only carry a spendable
-- closing; the mortgage loan accounts (liquid=FALSE) are excluded from cash.
ranked AS (
    SELECT
        week_start_date,
        entity,
        capacity,
        account_id,
        liquid,
        running_balance,
        ROW_NUMBER() OVER (
            PARTITION BY account_id, week_start_date
            ORDER BY txn_date DESC, posted_order DESC NULLS LAST, id DESC
        ) AS rn
    FROM categorised
    WHERE running_balance IS NOT NULL
),
week_close AS (
    SELECT
        week_start_date,
        entity,
        capacity,
        SUM(running_balance) FILTER (WHERE liquid) AS closing_liquid,
        SUM(running_balance) AS closing_all
    FROM ranked
    WHERE rn = 1
    GROUP BY week_start_date, entity, capacity
),
keys AS (
    SELECT week_start_date, entity, capacity FROM flows
    UNION
    SELECT week_start_date, entity, capacity FROM week_close
)
SELECT
    k.week_start_date,
    (k.week_start_date + INTERVAL '6 days')::date AS week_end_date,
    k.entity,
    k.capacity,
    COALESCE(fin.amount_total, 0)     AS inflows_total,
    COALESCE(fin.amount_external, 0)  AS inflows_external,
    COALESCE(fout.amount_total, 0)    AS outflows_total,
    COALESCE(fout.amount_external, 0) AS outflows_external,
    COALESCE(fin.txn_count, 0)        AS inflow_txn_count,
    COALESCE(fout.txn_count, 0)       AS outflow_txn_count,
    wc.closing_liquid,
    wc.closing_all
FROM keys k
LEFT JOIN flows fin
    ON fin.week_start_date = k.week_start_date
   AND fin.entity = k.entity AND fin.capacity = k.capacity
   AND fin.direction = 'CREDIT'
LEFT JOIN flows fout
    ON fout.week_start_date = k.week_start_date
   AND fout.entity = k.entity AND fout.capacity = k.capacity
   AND fout.direction = 'DEBIT'
LEFT JOIN week_close wc
    ON wc.week_start_date = k.week_start_date
   AND wc.entity = k.entity AND wc.capacity = k.capacity;
"""

# Reverse: restore the 0026 definition (bare INSTAL token) so a rollback is exact.
REVERSE_SQL = r"""
CREATE OR REPLACE VIEW v_cash_actuals_weekly AS
WITH attr AS (
    SELECT * FROM (VALUES
        ('10011910139',   'MC personal',        'personal', TRUE),
        ('1100588588501', 'MC personal',        'personal', TRUE),
        ('1100588588540', 'MC personal',        'personal', TRUE),
        ('10014792677',   'L Dippenaar',        'personal', TRUE),
        ('1100116634500', 'L Dippenaar',        'personal', TRUE),
        ('10011924075',   'Klikk',              'business', TRUE),
        ('363177001',     'Klikk',              'business', FALSE),
        ('363177003',     'Klikk',              'business', FALSE),
        ('10013017883',   'MLD Trust',          'business', TRUE),
        ('1100591058540', 'MLD Trust',          'business', TRUE),
        ('10013648134',   'Testamentary Trust', 'business', TRUE),
        ('1100603381540', 'Testamentary Trust', 'business', TRUE),
        ('10014572054',   'Lucanaude',          'business', TRUE),
        ('1100612949540', 'Lucanaude',          'business', TRUE)
    ) AS a(account_number, entity, capacity, liquid)
),
txn AS (
    SELECT
        t.id, t.account_id, acc.account_number, attr.entity, attr.capacity, attr.liquid,
        t.type AS direction,
        COALESCE(t.transaction_date, t.posting_date) AS txn_date,
        date_trunc('week', COALESCE(t.transaction_date, t.posting_date))::date AS week_start_date,
        t.amount, t.running_balance, t.posted_order,
        upper(COALESCE(t.description, '')) AS udesc, t.transaction_type
    FROM investec_investecbanktransaction t
    JOIN investec_investecbankaccount acc ON acc.id = t.account_id
    JOIN attr ON attr.account_number = acc.account_number
    WHERE COALESCE(t.transaction_date, t.posting_date) IS NOT NULL
),
categorised AS (
    SELECT txn.*,
        CASE
            WHEN udesc ~ '(10011910139|1100588588501|1100588588540|10014792677|1100116634500|10011924075|363177001|363177003|10013017883|1100591058540|10013648134|1100603381540|10014572054|1100612949540)'
                THEN 'transfer_internal'
            WHEN direction = 'CREDIT' AND udesc ~ 'PERESEC|DIVIDEND|DIV PMT|LEGAEPERE' THEN 'jse_dividend'
            WHEN direction = 'CREDIT' AND udesc ~ 'ANCHOR|BCI CORE|WESTBROOKE|FLEXIBLE INCOME' THEN 'anchor_income'
            WHEN direction = 'CREDIT' AND udesc ~ 'RENT|HUUR|LEASE|TENANT|BOERDERY|RENTAL' THEN 'rental_income'
            WHEN direction = 'DEBIT' AND udesc ~ 'VAT201|VALUE ADDED|(^|[^A-Z])VAT([^A-Z]|$)' THEN 'vat'
            WHEN direction = 'DEBIT' AND udesc ~ 'FLEXIBOND|MORTGAGE|BOND REPAYMENT|LOAN REPAYMENT|FACILITY|INSTAL|FINANCE CHARGE|INTEREST CHARGED' THEN 'debt_service'
            WHEN direction = 'DEBIT' AND udesc ~ 'TREMLY|MLD TRUST|MANAGEMENT FEE|MGMT FEE|LUCANAUDE|LUCA NAUDE' THEN 'related_party'
            WHEN direction = 'DEBIT' AND transaction_type = 'CardPurchases' THEN 'operating_outflow'
            WHEN direction = 'DEBIT' AND transaction_type = 'ATMWithdrawals' THEN 'personal_drawing'
            WHEN direction = 'DEBIT' AND transaction_type = 'FeesAndInterest' THEN 'debt_service'
            WHEN direction = 'DEBIT' AND transaction_type = 'DebitOrders' THEN 'operating_outflow'
            WHEN direction = 'DEBIT' AND transaction_type = 'BudgetInstalments' THEN 'debt_service'
            WHEN direction = 'DEBIT' AND capacity = 'personal' THEN 'personal_drawing'
            WHEN direction = 'DEBIT' THEN 'operating_outflow'
            ELSE 'other'
        END AS category
    FROM txn
),
flows AS (
    SELECT week_start_date, entity, capacity, direction,
        SUM(amount) AS amount_total,
        SUM(amount) FILTER (WHERE category NOT IN ('transfer_internal', 'related_party')) AS amount_external,
        COUNT(*) AS txn_count
    FROM categorised
    GROUP BY week_start_date, entity, capacity, direction
),
ranked AS (
    SELECT week_start_date, entity, capacity, account_id, liquid, running_balance,
        ROW_NUMBER() OVER (
            PARTITION BY account_id, week_start_date
            ORDER BY txn_date DESC, posted_order DESC NULLS LAST, id DESC
        ) AS rn
    FROM categorised WHERE running_balance IS NOT NULL
),
week_close AS (
    SELECT week_start_date, entity, capacity,
        SUM(running_balance) FILTER (WHERE liquid) AS closing_liquid,
        SUM(running_balance) AS closing_all
    FROM ranked WHERE rn = 1
    GROUP BY week_start_date, entity, capacity
),
keys AS (
    SELECT week_start_date, entity, capacity FROM flows
    UNION
    SELECT week_start_date, entity, capacity FROM week_close
)
SELECT
    k.week_start_date,
    (k.week_start_date + INTERVAL '6 days')::date AS week_end_date,
    k.entity, k.capacity,
    COALESCE(fin.amount_total, 0)     AS inflows_total,
    COALESCE(fin.amount_external, 0)  AS inflows_external,
    COALESCE(fout.amount_total, 0)    AS outflows_total,
    COALESCE(fout.amount_external, 0) AS outflows_external,
    COALESCE(fin.txn_count, 0)        AS inflow_txn_count,
    COALESCE(fout.txn_count, 0)       AS outflow_txn_count,
    wc.closing_liquid, wc.closing_all
FROM keys k
LEFT JOIN flows fin
    ON fin.week_start_date = k.week_start_date AND fin.entity = k.entity AND fin.capacity = k.capacity AND fin.direction = 'CREDIT'
LEFT JOIN flows fout
    ON fout.week_start_date = k.week_start_date AND fout.entity = k.entity AND fout.capacity = k.capacity AND fout.direction = 'DEBIT'
LEFT JOIN week_close wc
    ON wc.week_start_date = k.week_start_date AND wc.entity = k.entity AND wc.capacity = k.capacity;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("investec", "0027_cashflow_forecast"),
    ]

    operations = [
        migrations.RunSQL(CREATE_VIEW_SQL, REVERSE_SQL),
    ]
