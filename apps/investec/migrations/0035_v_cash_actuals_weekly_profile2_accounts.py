"""Add the four credential-profile-2 accounts to v_cash_actuals_weekly.

The view inlines the account -> entity/capacity/liquid table that
apps/investec/owner_map.py also holds; the two must move together or an account
syncs into the database and then silently vanishes from the cash-actuals view
(it is an INNER JOIN on attr). This carries the same four rows the owner map
gained:

  T Naudé   10012985320 / 1100007340540   personal, liquid=TRUE
  L Naudé   10014870332 / 1100123814500   personal, liquid=FALSE

T Naudé's are ordinary transactional accounts and count toward group liquid
cash. L Naudé's are a Youth Account and a PrimeSaver — real balances, but not
spendable group liquidity, so liquid=FALSE keeps them out of closing_liquid
while closing_all still sees them.

The four numbers also join the internal-transfer regex, so money moving between
these accounts and the existing fourteen eliminates at group level instead of
showing up as external flow on both sides.

Nothing else in the view changes. CREATE OR REPLACE VIEW with an identical
column list is metadata-only — no rebuild, no lock on the transaction table.
Zero-downtime classification: trivial.

Reverse restores the 0029 definition (the fourteen-account table).
"""
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
        ('1100612949540', 'Lucanaude',          'business', TRUE),
        ('10012985320',   'T Naudé',            'personal', TRUE),
        ('1100007340540', 'T Naudé',            'personal', TRUE),
        ('10014870332',   'L Naudé',            'personal', FALSE),
        ('1100123814500', 'L Naudé',            'personal', FALSE)
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
        t.posting_date,
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
            -- 1. internal transfer (any of the 18 account numbers in the desc) — eliminates
            WHEN udesc ~ '(10011910139|1100588588501|1100588588540|10014792677|1100116634500|10011924075|363177001|363177003|10013017883|1100591058540|10013648134|1100603381540|10014572054|1100612949540|10012985320|1100007340540|10014870332|1100123814500)'
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
-- Week-end closing balance per account = the MOST-RECENT running_balance in the
-- week. "Most recent" = latest posting_date (the bank's settlement/sequence date
-- that running_balance advances with), then highest posted_order, then highest
-- id. posting_date is COALESCEd with transaction_date only so a value-only row
-- still sorts. Liquid accounts only carry a spendable closing; the mortgage loan
-- accounts (liquid=FALSE) are excluded from cash.
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
            ORDER BY COALESCE(posting_date, txn_date) DESC, posted_order DESC NULLS LAST, id DESC
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


# Reverse: the 0029 definition, unchanged.
REVERSE_SQL = r"""
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
        t.posting_date,
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
-- Week-end closing balance per account = the MOST-RECENT running_balance in the
-- week. "Most recent" = latest posting_date (the bank's settlement/sequence date
-- that running_balance advances with), then highest posted_order, then highest
-- id. posting_date is COALESCEd with transaction_date only so a value-only row
-- still sorts. Liquid accounts only carry a spendable closing; the mortgage loan
-- accounts (liquid=FALSE) are excluded from cash.
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
            ORDER BY COALESCE(posting_date, txn_date) DESC, posted_order DESC NULLS LAST, id DESC
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


class Migration(migrations.Migration):
    dependencies = [
        ('investec', '0034_investecjsesharenamemapping_mapped_by_and_more'),
    ]
    operations = [
        migrations.RunSQL(sql=CREATE_VIEW_SQL, reverse_sql=REVERSE_SQL),
    ]
