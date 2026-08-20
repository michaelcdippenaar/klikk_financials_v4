"""
Seed content for ``audit.checks`` — the v1 registry from
"Klikk (Pty) Ltd — Year-End Audit Procedures & Check Registry" (19 Aug 2026).

Conventions baked into every SQL (do not break them):
  * tenant        -> ``:tenant_id`` (Klikk = 41ebfa0e-012e-4ff1-82ba-a9a7585c536c)
  * FY window     -> ``:fy_start`` .. ``:fy_end`` (inclusive dates; journals are
                     timestamptz so we compare ``date < :fy_end::date + 1``)
  * journals      -> ``xero_data_xerojournals``; every journal exists in two
                     flavours. ``journal_type='transaction'`` (per-source feed:
                     carries contact_id / transaction_source_id) is used for
                     supplier/contact analysis; ``'manual_journal'`` for manual
                     journals; ``'journal'`` (Journals endpoint = the complete GL,
                     incl. conversion balances — transaction+manual do NOT sum to
                     it) for balances, TB and duplicate-ingestion checks.
                     Credits are stored NEGATIVE.
  * bank          -> ``investec_investecbankaccount`` account_name 'Klikk (Pty) Ltd'
                     (product 'Investec Private Business Account') = business,
                     'Mr MC Dippenaar' = personal; ``type='DEBIT'`` = outflow;
                     always ``transaction_date`` (posting_date is unreliable).
  * slips         -> ``whatsapp.klikk_slips`` / ``whatsapp.v_slips_xero``
  * contacts      -> ``xero_metadata_xerocontacts.contacts_id``
  * accounts      -> ``xero_metadata_xeroaccount`` (type, code, name)
  * 'value' checks return >=1 row with a boolean column ``ok`` (all must be true).

Where the source data a check really needs does not exist yet (mailbox intake,
supplier statements, lease register, contract end dates) the SQL implements the
best available proxy and the description says so explicitly ("GAP:").
"""

SOURCE = 'Klikk-YearEnd-Audit-Procedures.md v1 (2026-08-19)'

# Reusable fragments --------------------------------------------------------
_PAY_DATE = "to_timestamp(substring(s.collection->>'Date' from '[0-9]+')::bigint / 1000)::date"
_EXPENSE_TYPES = "('EXPENSE','OVERHEADS','DIRECTCOSTS')"
_SPEND_TYPES = "('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED')"
_XERO_BANK = """xb AS (
  SELECT account_id FROM xero_metadata_xeroaccount
  WHERE organisation_id = :tenant_id AND type = 'BANK' AND name = 'Klikk (Pty) Ltd')"""
_INVESTEC_BUSINESS = """ib AS (
  SELECT id FROM investec_investecbankaccount
  WHERE account_name = 'Klikk (Pty) Ltd' AND product_name = 'Investec Private Business Account')"""
_INVESTEC_PERSONAL = """ip AS (
  SELECT id FROM investec_investecbankaccount WHERE account_name = 'Mr MC Dippenaar')"""
_SUPPLIER_TOKENS = """sup AS (
  -- supplier "token" = first word of the contact name if it is >= 6 chars, else first two words;
  -- matched against the bank description (whitespace-collapsed, upper-cased)
  SELECT c.contacts_id, c.name, w.w1,
         CASE WHEN length(w.w1) >= 6 OR w.w2 = '' THEN w.w1 ELSE w.w1 || ' ' || w.w2 END AS tok
  FROM xero_metadata_xerocontacts c
  CROSS JOIN LATERAL (
    SELECT split_part(n, ' ', 1) AS w1, split_part(n, ' ', 2) AS w2
    FROM (SELECT upper(trim(regexp_replace(regexp_replace(c.name, '[^A-Za-z0-9 ]', ' ', 'g'), '\\s+', ' ', 'g'))) AS n) x) w
  WHERE c.organisation_id = :tenant_id
    AND EXISTS (SELECT 1 FROM xero_data_xerojournals j
                WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
                  AND j.contact_id = c.contacts_id AND j.debit > 0
                  AND j.date >= :fy_start::date - 365)),
supt AS (
  SELECT * FROM sup
  WHERE length(tok) >= 4
    AND w1 NOT IN ('INVESTEC','STANDARD','UNKNOWN','CASH','PAYMENT','SARS','CITY','KLIKK','TREMLY',
                   'DIPPENAAR','MISS','UBER','SHELL','TOTAL','ENGEN','CAPITEC','NEDBANK','ABSA','THE',
                   'MUNICIPALITY','STELLENBOSCH','PERSONAL','GENERAL','OTHER','SUNDRY','TRUST','TRANSFER'))"""


CHECKS = [
    # ----------------------------------------------------------------- A. RDY
    dict(
        code='RDY-01', category='RDY', severity='high', expected='value', owner_action='engineering',
        title='Xero journal mirror fresh',
        description='Latest journal date in xero_data_xerojournals for the tenant must be >= today - 2 days. '
                    'A stale mirror makes every other check unreliable.',
        rationale='Data-readiness gate (run first).',
        sql_text="""
SELECT 'latest journal date' AS value,
       max(date)::date AS latest_journal_date,
       current_date AS today,
       (current_date - max(date)::date) AS days_stale,
       (max(date)::date >= current_date - 2) AS ok
FROM xero_data_xerojournals
WHERE organisation_id = :tenant_id
""",
    ),
    dict(
        code='RDY-02', category='RDY', severity='high', expected='zero_rows', owner_action='engineering',
        title='Investec mirror fresh (business + personal transactional accounts)',
        description='Lists the Klikk business account and MC personal current account if their latest '
                    'transaction_date is older than 3 days (or missing). Mortgage / savers are excluded '
                    '(monthly movement).',
        rationale='Data-readiness gate; BNK-* checks need both accounts current.',
        sql_text="""
SELECT a.account_name, a.account_number, a.product_name,
       max(t.transaction_date) AS latest_tx,
       current_date - max(t.transaction_date) AS days_stale
FROM investec_investecbankaccount a
LEFT JOIN investec_investecbanktransaction t ON t.account_id = a.id
WHERE a.account_name IN ('Klikk (Pty) Ltd', 'Mr MC Dippenaar')
  AND a.product_name IN ('Investec Private Business Account', 'Private Bank Account')
GROUP BY 1, 2, 3
HAVING max(t.transaction_date) IS NULL OR max(t.transaction_date) < current_date - 3
""",
    ),
    dict(
        code='RDY-03', category='RDY', severity='critical', expected='value', owner_action='engineering',
        title='Trial balance nets to zero per FY (each journal flavour)',
        description='sum(debit)+sum(credit) for the FY must be 0.00 (tolerance 0.05) for each journal_type '
                    'ingested (journal / transaction / manual_journal). Catches dropped VAT legs and half journals.',
        rationale='Aug 2026 bug: input VAT leg silently dropped on every purchase (-R371k) + 4 half-excluded payroll journals.',
        sql_text="""
SELECT journal_type AS value,
       count(*) AS lines,
       round(sum(debit), 2) AS total_debit,
       round(sum(credit), 2) AS total_credit,
       round(sum(debit) + sum(credit), 2) AS net,
       abs(round(sum(debit) + sum(credit), 2)) < 0.05 AS ok
FROM xero_data_xerojournals
WHERE organisation_id = :tenant_id
  AND date >= :fy_start AND date < :fy_end::date + 1
GROUP BY 1
ORDER BY 1
""",
    ),
    dict(
        code='RDY-04', category='RDY', severity='low', expected='list', owner_action='engineering',
        title='Void/reversal pairs in the retired journal mirror (informational)',
        description='RETIRED FEED — informational only (2026-08-20). Journal numbers in the frozen '
                    'journal_type=journal mirror where every line appears exactly twice (Boschendal x2 bug). '
                    'Xero moved the Journals API to the Advanced tier in Mar 2026, so this mirror is frozen at '
                    '2025-11-25 and no longer feeds the trial balance or the cube, meaning findings here do NOT '
                    'affect reported balances. Verified 2026-08-20: all 356 voided manual journals net to exactly '
                    'R0.00 in the mirror (original + reversal both present), and voided/deleted manual journals '
                    'get no legs in the live feed at all.',
        rationale='Was: klikk-xero-reconciliation-bridge -- voided manual journals ingested '
                  '(Boschendal exactly double). DISPROVEN 2026-08-20: the mirror carries BOTH the '
                  'original and its reversal, so every such group nets to exactly R0.00 (verified on '
                  'all 356 voided manual journals, and on the VOX 3D group 42340/42341/42366/42367 = '
                  'R0.00 across 44 lines). The live ledger omits voided documents entirely. Nothing is '
                  'overstated; this lists the void trail only.',
        sql_text="""
SELECT journal_number,
       min(date)::date AS jdate,
       count(*) AS lines,
       round(sum(debit), 2) AS total_debit,
       max(description) AS sample_description,
       max(reference) AS reference
FROM xero_data_xerojournals
WHERE organisation_id = :tenant_id AND journal_type = 'journal'
  AND date >= :fy_start AND date < :fy_end::date + 1
GROUP BY journal_number
HAVING count(*) = 2 * count(DISTINCT (account_id, debit, credit, coalesce(description, '')))
   AND count(*) > count(DISTINCT (account_id, debit, credit, coalesce(description, '')))
ORDER BY total_debit DESC
""",
    ),
    dict(
        code='RDY-05', category='RDY', severity='medium', expected='value', owner_action='engineering',
        title='Aged payables / receivables tables populated',
        description='xero_data_agedpayable and xero_data_agedreceivable must hold rows for the tenant '
                    '(found empty 18 Aug 2026). BAL-06/07 fall back to the invoice table when empty.',
        rationale='Empty aged tables found 18 Aug 2026.',
        sql_text="""
SELECT 'aged_payables' AS value, count(*) AS rows, max(report_date) AS latest_report, count(*) > 0 AS ok
FROM xero_data_agedpayable WHERE tenant_id = :tenant_id
UNION ALL
SELECT 'aged_receivables', count(*), max(report_date), count(*) > 0
FROM xero_data_agedreceivable WHERE tenant_id = :tenant_id
""",
    ),
    dict(
        code='RDY-06', category='RDY', severity='medium', expected='zero_rows', owner_action='engineering',
        title='Attachment coverage known (no has_attachments NULL)',
        description='Transaction sources of type Invoice / BankTransaction / CreditNote with has_attachments IS NULL '
                    '— attachment discovery has not run for them, so DOC-* checks under-report.',
        rationale='Attachment discovery via HasAttachments list field (2026-08-19).',
        sql_text="""
SELECT transaction_source, count(*) AS rows_without_flag, min(id) AS first_id, max(id) AS last_id
FROM xero_data_xerotransactionsource
WHERE organisation_id = :tenant_id
  AND transaction_source IN ('Invoice', 'BankTransaction', 'CreditNote')
  AND has_attachments IS NULL
GROUP BY 1
""",
    ),
    dict(
        code='RDY-07', category='RDY', severity='high', expected='value', owner_action='MC',
        title='Xero connection healthy (no reauth_required, refresh token present)',
        description='Tenant must not be flagged reauth_required and an active credential with a refresh token must exist.',
        rationale='Dead token = silently stale mirror.',
        sql_text="""
SELECT t.tenant_name AS value,
       t.reauth_required,
       t.reauth_reason,
       t.reauth_flagged_at,
       (SELECT max(expires_at) FROM xero_auth_xerotenanttoken tt WHERE tt.tenant_id = t.tenant_id) AS access_token_expires_at,
       EXISTS (SELECT 1 FROM xero_auth_xeroclientcredentials c WHERE c.active AND coalesce(c.refresh_token, '') <> '') AS refresh_token_present,
       (NOT coalesce(t.reauth_required, false)
        AND EXISTS (SELECT 1 FROM xero_auth_xeroclientcredentials c WHERE c.active AND coalesce(c.refresh_token, '') <> '')) AS ok
FROM xero_core_xerotenant t
WHERE t.tenant_id = :tenant_id
""",
    ),

    # ----------------------------------------------------------------- B. DOC
    dict(
        code='DOC-01', category='DOC', severity='high', expected='list', owner_action='bookkeeper',
        title='Slips with no Xero match (non-meals)',
        description='Slippies-register slips dated in the FY, not synced to Xero (status PENDING / NOT IN XERO), '
                    'excluding meal/restaurant categories. Uncaptured purchases → bookkeeper captures.',
        rationale='Slip reconciliation FY2026.',
        sql_text="""
SELECT s.slip_ts::date AS slip_date, s.slip_supplier, s.slip_total, s.slip_category, s.xero_status, s.filename, s.sha256
FROM whatsapp.v_slips_xero s
WHERE s.slip_ts >= :fy_start AND s.slip_ts < :fy_end::date + 1
  AND NOT coalesce(s.synced_to_xero, false)
  AND coalesce(s.xero_status, '') NOT ILIKE 'skipped%'
  AND coalesce(s.slip_category, '') !~* '(meal|restaurant|dining|coffee|fast food|takeaway|take-away)'
ORDER BY s.slip_ts
""",
    ),
    dict(
        code='DOC-02', category='DOC', severity='low', expected='list', owner_action='MC',
        title='Slips with no Xero match (meals — policy pile)',
        description='Meal / restaurant slips in the FY not synced to Xero. Not captured pending MC policy decision '
                    '(business entertainment vs personal / loan account).',
        rationale='Slip reconciliation FY2026 — meals policy undecided.',
        sql_text="""
SELECT s.slip_ts::date AS slip_date, s.slip_supplier, s.slip_total, s.slip_category, s.xero_status, s.filename, s.sha256
FROM whatsapp.v_slips_xero s
WHERE s.slip_ts >= :fy_start AND s.slip_ts < :fy_end::date + 1
  AND NOT coalesce(s.synced_to_xero, false)
  AND coalesce(s.xero_status, '') NOT ILIKE 'skipped%'
  AND coalesce(s.slip_category, '') ~* '(meal|restaurant|dining|coffee|fast food|takeaway|take-away)'
ORDER BY s.slip_ts
""",
    ),
    dict(
        code='DOC-03', category='DOC', severity='medium', expected='list', owner_action='bookkeeper',
        title='Expense/asset lines >= R1,000 with no slip AND no Xero attachment',
        description='Debit lines >= R1,000 on expense / fixed-asset accounts in the FY whose source document '
                    '(Invoice or BankTransaction) has no Xero attachment and no slip in the register (by journal '
                    'number or by amount within +-5 days). Excludes accounts that have no voucher by nature '
                    '(depreciation, payroll, bank fees, interest, tax).',
        rationale='Voucher-less spend is the auditor\'s first question.',
        sql_text="""
SELECT j.journal_number, j.date::date AS jdate, c.name AS contact, a.name AS account,
       left(j.description, 120) AS description, j.debit, j.tax_amount,
       s.transaction_source AS source_type
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
LEFT JOIN xero_metadata_xerocontacts c ON c.contacts_id = j.contact_id
LEFT JOIN xero_data_xerotransactionsource s ON s.transactions_id = j.transaction_source_id
WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  AND j.debit >= 1000
  AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED')
  AND a.name !~* '(depreciation|amortisation|salar|wages|uif|sdl|bank fee|interest|impairment|tax|management fee|finance cost|rounding|revaluation|currency)'
  AND coalesce(s.has_attachments, false) = false
  AND NOT EXISTS (SELECT 1 FROM whatsapp.klikk_slips k WHERE k.journal_number = j.journal_number)
  AND NOT EXISTS (SELECT 1 FROM whatsapp.klikk_slips k
                  WHERE k.slip_ts::date BETWEEN j.date::date - 5 AND j.date::date + 5
                    AND (k.ocr->>'total') ~ '^[0-9]+\\.?[0-9]*$'
                    AND abs((k.ocr->>'total')::numeric - (j.debit + coalesce(j.tax_amount, 0))) < 0.01)
ORDER BY j.debit DESC
""",
    ),
    dict(
        code='DOC-04', category='DOC', severity='medium', expected='list', owner_action='MC',
        title='Recurring suppliers whose bills never carry a source document (intake bypass proxy)',
        description='GAP: mailbox data (mc@ vs accounts@) is not in Postgres yet, so this is a PROXY: suppliers with '
                    '>= 3 bills in the FY where one or more bills have no Xero attachment. If the invoice reached '
                    'accounts@ it would normally be attached — missing docs per recurring supplier point at invoices '
                    'landing in mc@ and never forwarded. Replace with a real mailbox-intake table when available.',
        rationale='Intake bypass (invoices in mc@ never forwarded to accounts@).',
        sql_text="""
SELECT i.contact_name AS supplier,
       count(*) AS bills_in_fy,
       count(*) FILTER (WHERE NOT coalesce(i.has_attachments, false)) AS bills_without_doc,
       round(sum(i.total) FILTER (WHERE NOT coalesce(i.has_attachments, false)), 2) AS amount_without_doc,
       left(string_agg(i.invoice_number, ', ' ORDER BY i.date) FILTER (WHERE NOT coalesce(i.has_attachments, false)), 200) AS missing_bill_numbers
FROM xero_data_xeroinvoice i
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
  AND i.status IN ('PAID', 'AUTHORISED')
  AND i.date BETWEEN :fy_start AND :fy_end
GROUP BY 1
HAVING count(*) >= 3 AND count(*) FILTER (WHERE NOT coalesce(i.has_attachments, false)) > 0
ORDER BY amount_without_doc DESC NULLS LAST
""",
    ),
    dict(
        code='DOC-05', category='DOC', severity='medium', expected='list', owner_action='bookkeeper',
        title='Xero bills without attachment, coverage % by supplier',
        description='Per supplier: ACCPAY bills (PAID/AUTHORISED) in the FY, how many carry an attachment, coverage %, '
                    'and the bill numbers missing a document. Only suppliers with at least one missing doc are listed.',
        rationale='Missing source documents.',
        sql_text="""
SELECT i.contact_name AS supplier,
       count(*) AS bills,
       count(*) FILTER (WHERE coalesce(i.has_attachments, false)) AS with_attachment,
       round(100.0 * count(*) FILTER (WHERE coalesce(i.has_attachments, false)) / count(*), 0) AS coverage_pct,
       round(sum(i.total) FILTER (WHERE NOT coalesce(i.has_attachments, false)), 2) AS amount_missing_doc,
       left(string_agg(i.invoice_number || ' (' || i.date || ' R' || i.total || ')', '; ' ORDER BY i.date)
            FILTER (WHERE NOT coalesce(i.has_attachments, false)), 300) AS missing_bills
FROM xero_data_xeroinvoice i
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
  AND i.status IN ('PAID', 'AUTHORISED')
  AND i.date BETWEEN :fy_start AND :fy_end
GROUP BY 1
HAVING count(*) FILTER (WHERE NOT coalesce(i.has_attachments, false)) > 0
ORDER BY coverage_pct, bills DESC
""",
    ),
    dict(
        code='DOC-06', category='DOC', severity='medium', expected='zero_rows', owner_action='engineering',
        title='Slippies group messages with no file in the register',
        description='Image/document messages in the Slippies WhatsApp group (FY window) that have neither a stored '
                    'attachment nor a slip row within +-3 minutes — media lost before it reached the register.',
        rationale='whatsapp.klikk_slips_missing view; MISSING windows Feb–Apr 2026, 1–13 May 2026.',
        sql_text="""
SELECT message_id, ts, sender, media_type, filename
FROM whatsapp.klikk_slips_missing
WHERE ts >= :fy_start AND ts < :fy_end::date + 1
ORDER BY ts
""",
    ),

    # ----------------------------------------------------------------- C. BNK
    dict(
        code='BNK-01', category='BNK', severity='high', expected='zero_rows', owner_action='bookkeeper',
        title='Klikk business-account debits with no Xero bank journal (+-5d, same amount)',
        description='Investec business-account DEBITs in the FY with no journal line on the Xero bank account '
                    '"Klikk (Pty) Ltd" for the same amount within +-5 days. Unbooked business payments.',
        rationale='Bank ↔ books, bank side.',
        sql_text=f"""
WITH {_XERO_BANK},
{_INVESTEC_BUSINESS}
SELECT t.transaction_date, t.transaction_type, t.amount, t.description
FROM investec_investecbanktransaction t
WHERE t.account_id IN (SELECT id FROM ib)
  AND t.type = 'DEBIT'
  AND t.transaction_date BETWEEN :fy_start AND :fy_end
  AND NOT EXISTS (
      SELECT 1 FROM xero_data_xerojournals j
      WHERE j.organisation_id = :tenant_id
        AND j.account_id IN (SELECT account_id FROM xb)
        AND j.credit < 0 AND abs(j.credit) = t.amount
        AND j.date::date BETWEEN t.transaction_date - 5 AND t.transaction_date + 5)
ORDER BY t.amount DESC, t.transaction_date
""",
    ),
    dict(
        code='BNK-02', category='BNK', severity='high', expected='list', owner_action='MC',
        title='Personal-account debits to known Klikk suppliers with no Klikk journal',
        description='DEBITs >= R1,000 on MC\'s personal Investec accounts whose description contains the first word of a '
                    'known Klikk supplier (contact with debit lines in the last ~2 FYs) and no Klikk journal of the same '
                    'amount within +-5 days. The DME R90k / Aurras R64k / Expedition R24k pattern: business spend paid '
                    'personally and never booked (expense + shareholder loan). MC classifies business vs personal.',
        rationale='Allocation audit 2026-08-17: R393k personal payments missing from books.',
        sql_text=f"""
WITH {_SUPPLIER_TOKENS},
{_INVESTEC_PERSONAL}
SELECT DISTINCT ON (t.id)
       t.transaction_date, t.amount, t.description, s.name AS matched_supplier, t.transaction_type
FROM investec_investecbanktransaction t
JOIN supt s ON regexp_replace(upper(t.description), '\\s+', ' ', 'g') LIKE '%' || s.tok || '%'
WHERE t.account_id IN (SELECT id FROM ip)
  AND t.type = 'DEBIT'
  AND t.amount >= 1000
  AND t.transaction_date BETWEEN :fy_start AND :fy_end
  AND NOT EXISTS (
      SELECT 1 FROM xero_data_xerojournals j
      WHERE j.organisation_id = :tenant_id
        AND j.debit = t.amount
        AND j.date::date BETWEEN t.transaction_date - 5 AND t.transaction_date + 5)
ORDER BY t.id, s.name
""",
    ),
    dict(
        code='BNK-03', category='BNK', severity='medium', expected='zero_rows', owner_action='bookkeeper',
        title='Klikk-account credits from suppliers (refunds) not in Xero',
        description='CREDITs on the Klikk business account whose description matches a known supplier and that have no '
                    'Xero journal debit of the same amount within +-5 days. Unbooked refunds.',
        rationale='Bank ↔ books, refunds direction.',
        sql_text=f"""
WITH {_SUPPLIER_TOKENS},
{_INVESTEC_BUSINESS}
SELECT DISTINCT ON (t.id)
       t.transaction_date, t.amount, t.description, s.name AS matched_supplier, t.transaction_type
FROM investec_investecbanktransaction t
JOIN supt s ON regexp_replace(upper(t.description), '\\s+', ' ', 'g') LIKE '%' || s.tok || '%'
WHERE t.account_id IN (SELECT id FROM ib)
  AND t.type = 'CREDIT'
  AND t.transaction_date BETWEEN :fy_start AND :fy_end
  AND NOT EXISTS (
      SELECT 1 FROM xero_data_xerojournals j
      WHERE j.organisation_id = :tenant_id
        AND j.debit = t.amount
        AND j.date::date BETWEEN t.transaction_date - 5 AND t.transaction_date + 5)
ORDER BY t.id, s.name
""",
    ),
    dict(
        code='BNK-04', category='BNK', severity='medium', expected='list', owner_action='bookkeeper',
        title='Xero bank-account journals with no Investec movement (phantom payments)',
        description='Journal lines on the Xero bank account "Klikk (Pty) Ltd" (transaction + manual journals) in the FY '
                    'with no Investec business-account transaction of the same amount within +-5 days.',
        rationale='Bank ↔ books, books side.',
        sql_text=f"""
WITH {_XERO_BANK},
{_INVESTEC_BUSINESS}
SELECT j.journal_type, j.journal_number, j.date::date AS jdate, left(j.description, 120) AS description,
       j.reference, j.debit, j.credit
FROM xero_data_xerojournals j
WHERE j.organisation_id = :tenant_id
  AND j.journal_type IN ('transaction', 'manual_journal')
  AND j.account_id IN (SELECT account_id FROM xb)
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  AND abs(j.debit + j.credit) >= 0.01
  AND NOT EXISTS (
      SELECT 1 FROM investec_investecbanktransaction t
      WHERE t.account_id IN (SELECT id FROM ib)
        AND t.amount = abs(j.debit + j.credit)
        AND t.transaction_date BETWEEN j.date::date - 5 AND j.date::date + 5)
ORDER BY abs(j.debit + j.credit) DESC
""",
    ),

    # ----------------------------------------------------------------- D. SUP
    dict(
        code='SUP-01', category='SUP', severity='high', expected='zero_rows', owner_action='bookkeeper',
        title='Payment referencing a bill that does not exist',
        description='ACCPAY payments (Payment sources) dated in the FY whose referenced bill (InvoiceID) is missing '
                    'from the invoice mirror or is VOIDED/DELETED. "Payment made – Bill X" with no bill X.',
        rationale='DME battery.',
        sql_text=f"""
SELECT s.collection->>'PaymentID' AS payment_id,
       {_PAY_DATE} AS pay_date,
       (s.collection->>'Amount')::numeric AS amount,
       s.collection->'Invoice'->>'InvoiceNumber' AS bill_number,
       s.collection->'Invoice'->'Contact'->>'Name' AS supplier,
       coalesce(i.status, 'NOT IN MIRROR') AS bill_status
FROM xero_data_xerotransactionsource s
LEFT JOIN xero_data_xeroinvoice i
       ON i.invoice_id = s.collection->'Invoice'->>'InvoiceID' AND i.organisation_id = :tenant_id
WHERE s.organisation_id = :tenant_id
  AND s.transaction_source = 'Payment'
  AND s.collection->'Invoice'->>'Type' = 'ACCPAY'
  AND coalesce(s.collection->>'Status', '') <> 'DELETED'
  AND {_PAY_DATE} BETWEEN :fy_start AND :fy_end
  AND (i.id IS NULL OR i.status IN ('DELETED', 'VOIDED'))
ORDER BY amount DESC
""",
    ),
    dict(
        code='SUP-02', category='SUP', severity='medium', expected='list', owner_action='bookkeeper',
        title='Payment dated before its bill',
        description='ACCPAY payments in the FY whose payment date precedes the bill date (DME 0605 pattern): either the '
                    'bill was back-created after paying from bank, or the wrong bill was allocated.',
        rationale='DME battery.',
        sql_text=f"""
SELECT i.contact_name AS supplier, i.invoice_number AS bill_number, i.date AS bill_date,
       {_PAY_DATE} AS pay_date,
       i.date - {_PAY_DATE} AS days_before_bill,
       (s.collection->>'Amount')::numeric AS amount, i.total AS bill_total, i.status AS bill_status
FROM xero_data_xerotransactionsource s
JOIN xero_data_xeroinvoice i
  ON i.invoice_id = s.collection->'Invoice'->>'InvoiceID' AND i.organisation_id = :tenant_id
WHERE s.organisation_id = :tenant_id
  AND s.transaction_source = 'Payment'
  AND s.collection->'Invoice'->>'Type' = 'ACCPAY'
  AND coalesce(s.collection->>'Status', '') <> 'DELETED'
  AND i.status NOT IN ('DELETED', 'VOIDED')
  AND {_PAY_DATE} BETWEEN :fy_start AND :fy_end
  AND {_PAY_DATE} < i.date
ORDER BY days_before_bill DESC, amount DESC
""",
    ),
    dict(
        code='SUP-03', category='SUP', severity='high', expected='list', owner_action='bookkeeper',
        title='Bill with 2+ payments (over-paid or equal-amount repeats)',
        description='ACCPAY bills dated in the FY with two or more payments where either the payments exceed the bill '
                    'total or two payments have the same amount. Double payment candidates.',
        rationale='DME battery.',
        sql_text=f"""
WITH pay AS (
  SELECT s.collection->'Invoice'->>'InvoiceID' AS invoice_id,
         (s.collection->>'Amount')::numeric AS amount,
         {_PAY_DATE} AS pay_date
  FROM xero_data_xerotransactionsource s
  WHERE s.organisation_id = :tenant_id AND s.transaction_source = 'Payment'
    AND s.collection->'Invoice'->>'Type' = 'ACCPAY'
    AND coalesce(s.collection->>'Status', '') <> 'DELETED')
SELECT i.contact_name AS supplier, i.invoice_number AS bill_number, i.date AS bill_date, i.total AS bill_total,
       count(*) AS payments, round(sum(p.amount), 2) AS total_paid,
       string_agg(p.pay_date || ' R' || p.amount, '; ' ORDER BY p.pay_date) AS payment_list
FROM xero_data_xeroinvoice i
JOIN pay p ON p.invoice_id = i.invoice_id
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
  AND i.status NOT IN ('DELETED', 'VOIDED')
  AND i.date BETWEEN :fy_start AND :fy_end
GROUP BY 1, 2, 3, 4
HAVING count(*) >= 2 AND (sum(p.amount) > i.total + 0.01 OR count(*) > count(DISTINCT p.amount))
ORDER BY sum(p.amount) - i.total DESC
""",
    ),
    dict(
        code='SUP-04', category='SUP', severity='medium', expected='list', owner_action='bookkeeper',
        title='Same supplier, same amount, within 7 days, different journals',
        description='Pairs of debit lines (>= R500, expense/asset accounts) for the same contact and amount in different '
                    'journals within 7 days. Duplicate-capture candidates — verify live-vs-voided in Xero before acting. '
                    'Payroll / casual-wage accounts are excluded (weekly identical amounts are normal).',
        rationale='Allocation audit: duplicates pending live-vs-voided check.',
        sql_text="""
WITH l AS (
  SELECT j.journal_number, j.date::date AS jdate, j.contact_id, j.debit, j.description, a.name AS account
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND j.contact_id IS NOT NULL AND j.debit >= 500
    AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED','CURRENT','NONCURRENT')
    AND a.name !~* '(salar|wages|casual|uif|sdl|loan)'
    AND j.date >= :fy_start AND j.date < :fy_end::date + 1)
SELECT DISTINCT c.name AS supplier, x.debit AS amount,
       x.journal_number AS journal_a, x.jdate AS date_a,
       y.journal_number AS journal_b, y.jdate AS date_b,
       x.account, left(x.description, 100) AS description
FROM l x
JOIN l y ON y.contact_id = x.contact_id AND y.debit = x.debit
        AND y.journal_number > x.journal_number
        AND y.jdate BETWEEN x.jdate - 7 AND x.jdate + 7
JOIN xero_metadata_xerocontacts c ON c.contacts_id = x.contact_id
ORDER BY amount DESC, supplier
""",
    ),
    dict(
        code='SUP-05', category='SUP', severity='high', expected='list', owner_action='bookkeeper',
        title='Payments allocated directly from bank with NO bill behind them (per supplier)',
        description='Bank SPEND transactions coded straight to expense/asset for a contact that is otherwise billed via '
                    'ACCPAY bills. Because no bill exists the supplier invoice can never be allocated later '
                    '(MoneyBadgers 10 Dec / 4 Jan). Bookkeeper creates the bills and re-allocates the payments.',
        rationale='MC, 19 Aug 2026.',
        sql_text="""
WITH spend AS (
  SELECT j.contact_id, j.journal_number, j.date::date AS jdate, sum(j.debit) AS amount, max(a.name) AS account
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  JOIN xero_data_xerotransactionsource s ON s.transactions_id = j.transaction_source_id
       AND s.transaction_source = 'BankTransaction' AND s.collection->>'Type' = 'SPEND'
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND j.debit > 0 AND j.contact_id IS NOT NULL
    AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED')
    AND a.name !~* '(bank fee|interest|finance cost|penalt)'
    AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  GROUP BY 1, 2, 3),
billed AS (
  SELECT DISTINCT coalesce(xero_contact_id, contact_id) AS contact_id
  FROM xero_data_xeroinvoice
  WHERE organisation_id = :tenant_id AND type = 'ACCPAY' AND status NOT IN ('DELETED', 'VOIDED'))
SELECT c.name AS supplier,
       (SELECT count(*) FROM xero_data_xeroinvoice i WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
          AND i.status NOT IN ('DELETED','VOIDED') AND coalesce(i.xero_contact_id, i.contact_id) = sp.contact_id
          AND i.date BETWEEN :fy_start AND :fy_end) AS bills_in_fy,
       count(*) AS direct_bank_payments,
       round(sum(sp.amount), 2) AS total,
       min(sp.jdate) AS first_date, max(sp.jdate) AS last_date,
       left(string_agg(sp.jdate || ' R' || sp.amount || ' (' || sp.account || ')', '; ' ORDER BY sp.jdate), 300) AS detail
FROM spend sp
JOIN billed b ON b.contact_id = sp.contact_id
JOIN xero_metadata_xerocontacts c ON c.contacts_id = sp.contact_id
GROUP BY c.name, sp.contact_id
ORDER BY total DESC
""",
    ),
    dict(
        code='SUP-06', category='SUP', severity='high', expected='zero_rows', owner_action='bookkeeper',
        title='Supplier invoice number reused (same contact, same number, live bills)',
        description='Two or more live ACCPAY bills (not VOIDED/DELETED) for the same contact with the same invoice number, '
                    'at least one dated in the FY. INV-035 reuse / CW Plumbers #19 pattern → paid twice.',
        rationale='Supplier audit FY2026.',
        sql_text="""
SELECT i.contact_name AS supplier, i.invoice_number,
       count(*) AS live_bills,
       string_agg(i.date || ' R' || i.total || ' ' || i.status, '; ' ORDER BY i.date) AS detail
FROM xero_data_xeroinvoice i
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
  AND i.status NOT IN ('DELETED', 'VOIDED')
  AND coalesce(i.invoice_number, '') <> ''
GROUP BY coalesce(i.xero_contact_id, i.contact_id), i.contact_name, i.invoice_number
HAVING count(*) > 1 AND bool_or(i.date BETWEEN :fy_start AND :fy_end)
ORDER BY live_bills DESC, supplier
""",
    ),
    dict(
        code='SUP-07', category='SUP', severity='medium', expected='list', owner_action='supplier',
        title='Supplier statement three-way tie (bank paid vs Xero paid) — proxy',
        description='GAP: supplier statements are not held as data yet, so this is the two-way leg of the three-way tie: '
                    'for every supplier with ACCPAY bills in the FY, Investec business-account debits whose description '
                    'contains the supplier\'s first word vs Xero payments + bank-spend for that contact in the FY. '
                    'Listed where the two differ by more than R1. When a statement is received, add it as the third leg.',
        rationale='Year-end procedure step 5.',
        sql_text=f"""
WITH {_SUPPLIER_TOKENS},
{_INVESTEC_BUSINESS},
xero_paid AS (
  SELECT j.contact_id, round(sum(j.debit), 2) AS xero_paid, count(*) AS xero_lines
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND j.contact_id IS NOT NULL AND j.debit > 0
    AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED','CURRENT','NONCURRENT')
    AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  GROUP BY 1),
bank_paid AS (
  SELECT s.contacts_id AS contact_id, round(sum(t.amount), 2) AS bank_paid, count(*) AS bank_lines
  FROM investec_investecbanktransaction t
  JOIN supt s ON regexp_replace(upper(t.description), '\\s+', ' ', 'g') LIKE '%' || s.tok || '%'
  WHERE t.account_id IN (SELECT id FROM ib) AND t.type = 'DEBIT'
    AND t.transaction_date BETWEEN :fy_start AND :fy_end
  GROUP BY 1)
SELECT c.name AS supplier, xp.xero_paid, xp.xero_lines, bp.bank_paid, bp.bank_lines,
       round(coalesce(bp.bank_paid, 0) - coalesce(xp.xero_paid, 0), 2) AS bank_minus_xero
FROM xero_paid xp
JOIN bank_paid bp ON bp.contact_id = xp.contact_id
JOIN xero_metadata_xerocontacts c ON c.contacts_id = xp.contact_id
WHERE abs(coalesce(bp.bank_paid, 0) - coalesce(xp.xero_paid, 0)) > 1
  AND EXISTS (SELECT 1 FROM xero_data_xeroinvoice i WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
              AND coalesce(i.xero_contact_id, i.contact_id) = xp.contact_id AND i.date BETWEEN :fy_start AND :fy_end)
ORDER BY abs(coalesce(bp.bank_paid, 0) - coalesce(xp.xero_paid, 0)) DESC
""",
    ),
    dict(
        code='SUP-08', category='SUP', severity='low', expected='list', owner_action='bookkeeper',
        title='Supplier name fragmentation (merge candidates)',
        description='Pairs of non-archived contacts that are identical ignoring case/punctuation, or share the same first '
                    'word (>= 5 letters), where both have been used on journals. "Carry on"/"Carry On", 4x BP.',
        rationale='Supplier audit FY2026.',
        sql_text="""
WITH c AS (
  SELECT contacts_id, name,
         lower(regexp_replace(name, '[^a-z0-9]', '', 'gi')) AS norm,
         lower(split_part(trim(name), ' ', 1)) AS first_tok
  FROM xero_metadata_xerocontacts
  WHERE organisation_id = :tenant_id
    AND coalesce(collection->>'ContactStatus', 'ACTIVE') <> 'ARCHIVED'),
used AS (
  SELECT contact_id, count(*) AS n, max(date)::date AS last_used
  FROM xero_data_xerojournals
  WHERE organisation_id = :tenant_id AND journal_type = 'transaction' AND contact_id IS NOT NULL
  GROUP BY 1)
SELECT a.name AS contact_a, b.name AS contact_b,
       CASE WHEN a.norm = b.norm THEN 'identical ignoring case/punctuation' ELSE 'same first word' END AS reason,
       ua.n AS uses_a, ua.last_used AS last_used_a, ub.n AS uses_b, ub.last_used AS last_used_b
FROM c a
JOIN c b ON a.contacts_id < b.contacts_id
        AND (a.norm = b.norm
             OR (a.first_tok = b.first_tok AND length(a.first_tok) >= 5
                 AND a.first_tok NOT IN ('investec','standard','stellenbosch','municipality','personal','sundry','unknown')))
JOIN used ua ON ua.contact_id = a.contacts_id
JOIN used ub ON ub.contact_id = b.contacts_id
WHERE greatest(ua.last_used, ub.last_used) >= :fy_start
ORDER BY reason, a.name
""",
    ),

    # ----------------------------------------------------------------- E. BAL
    dict(
        code='BAL-01', category='BAL', severity='high', expected='list', owner_action='MC',
        title='Deposits held by suppliers where supplier activity has ended',
        description='Asset-side deposit accounts (CURRENT/NONCURRENT, name contains "deposit") with a positive balance at '
                    'FY end per contact, where the contact\'s last journal activity is more than 90 days before FY end '
                    'and no refund has cleared the balance (balances from the live ledger — transaction + manual_journal; contact attribution from the '
                    'transaction feed). The MoneyBadgers lesson: correct when booked, wrong by staying. '
                    'GAP: no contract-end-date field yet — "last payment > 90 days" is the proxy.',
        rationale='MoneyBadgers R9,813.33 deposit (Sep 2025), lease ended Jan 2026, never recovered.',
        sql_text="""
WITH acct AS (
  SELECT j.account_id, sum(j.debit) + sum(j.credit) AS gl_balance, max(j.date)::date AS last_movement
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type <> 'journal'
    AND a.type IN ('CURRENT', 'NONCURRENT') AND a.name ILIKE '%deposit%'
    AND j.date < :fy_end::date + 1
  GROUP BY 1
  HAVING sum(j.debit) + sum(j.credit) > 0.01),
bycontact AS (
  SELECT j.account_id, j.contact_id, sum(j.debit) + sum(j.credit) AS contact_net, max(j.date)::date AS last_deposit_movement
  FROM xero_data_xerojournals j
  JOIN acct ON acct.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND j.contact_id IS NOT NULL AND j.date < :fy_end::date + 1
  GROUP BY 1, 2
  HAVING sum(j.debit) + sum(j.credit) > 0.01),
act AS (
  SELECT contact_id, max(date)::date AS last_activity
  FROM xero_data_xerojournals
  WHERE organisation_id = :tenant_id AND journal_type = 'transaction'
    AND contact_id IS NOT NULL AND date < :fy_end::date + 1
  GROUP BY 1)
SELECT a.code AS account_code, a.name AS account, c.name AS supplier,
       round(least(b.contact_net, acct.gl_balance), 2) AS deposit_balance,
       round(acct.gl_balance, 2) AS account_gl_balance,
       b.last_deposit_movement, act.last_activity AS supplier_last_activity,
       :fy_end::date - act.last_activity AS days_since_activity
FROM bycontact b
JOIN acct ON acct.account_id = b.account_id
JOIN xero_metadata_xeroaccount a ON a.account_id = b.account_id
JOIN xero_metadata_xerocontacts c ON c.contacts_id = b.contact_id
LEFT JOIN act ON act.contact_id = b.contact_id
WHERE coalesce(act.last_activity, b.last_deposit_movement) < :fy_end::date - 90
UNION ALL
SELECT a.code, a.name, NULL, round(acct.gl_balance, 2), round(acct.gl_balance, 2),
       acct.last_movement, NULL, :fy_end::date - acct.last_movement
FROM acct
JOIN xero_metadata_xeroaccount a ON a.account_id = acct.account_id
WHERE NOT EXISTS (SELECT 1 FROM bycontact b WHERE b.account_id = acct.account_id)
  AND acct.last_movement < :fy_end::date - 90
ORDER BY deposit_balance DESC
""",
    ),
    dict(
        code='BAL-02', category='BAL', severity='medium', expected='list', owner_action='bookkeeper',
        title='Supplier prepayments / credits not consumed within 90 days',
        description='Supplier-side overpayments (SPEND-OVERPAYMENT) and supplier credit notes (ACCPAYCREDIT) older than '
                    '90 days at FY end that still carry RemainingCredit, plus per-contact balances in the Prepayments '
                    'account with no movement for 90 days. Overpayments sitting unused.',
        rationale='Balance-sheet lifecycle.',
        sql_text=f"""
SELECT s.transaction_source || ' ' || coalesce(s.collection->>'Type', '') AS kind,
       s.collection->'Contact'->>'Name' AS contact,
       {_PAY_DATE} AS doc_date,
       (s.collection->>'Total')::numeric AS total,
       (s.collection->>'RemainingCredit')::numeric AS remaining_credit,
       s.collection->>'Status' AS status,
       coalesce(s.collection->>'CreditNoteNumber', s.collection->>'Reference', '') AS reference
FROM xero_data_xerotransactionsource s
WHERE s.organisation_id = :tenant_id
  AND s.transaction_source IN ('Overpayment', 'CreditNote')
  AND s.collection->>'Type' IN ('SPEND-OVERPAYMENT', 'ACCPAYCREDIT')
  AND coalesce((s.collection->>'RemainingCredit')::numeric, 0) > 0.01
  AND coalesce(s.collection->>'Status', '') NOT IN ('VOIDED', 'DELETED')
  AND {_PAY_DATE} < :fy_end::date - 90
UNION ALL
SELECT 'Prepayments account (GL balance)', a.name,
       max(j.date)::date, NULL, round(sum(j.debit) + sum(j.credit), 2), 'open', a.code
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
WHERE j.organisation_id = :tenant_id AND j.journal_type <> 'journal'
  AND a.name = 'Prepayments' AND j.date < :fy_end::date + 1
GROUP BY a.name, a.code
HAVING sum(j.debit) + sum(j.credit) > 0.01 AND max(j.date)::date < :fy_end::date - 90
ORDER BY remaining_credit DESC NULLS LAST
""",
    ),
    dict(
        code='BAL-03', category='BAL', severity='medium', expected='list', owner_action='MC',
        title='Tenant deposits held vs active leases (proxy: rent still being received)',
        description='GAP: no lease register in Postgres. Proxy: deposit liability accounts ("Deposit - <property>") with a '
                    'non-zero balance at FY end, matched to the property\'s Profit Center tracking option; listed when no '
                    'rental income was credited for that property in the 90 days before FY end (tenant gone, deposit not '
                    'refunded / forfeited), or when the account cannot be matched to a property at all.',
        rationale='Balance-sheet lifecycle — departed tenants.',
        sql_text="""
WITH dep AS (
  SELECT a.account_id, a.code, a.name,
         substring(a.name from '(The One|[0-9]+ [A-Za-z]+)') AS token,
         sum(j.debit) + sum(j.credit) AS balance,
         max(j.date)::date AS last_movement
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type <> 'journal'
    AND a.type = 'CURRLIAB' AND a.name ILIKE 'Deposit%'
    AND j.date < :fy_end::date + 1
  GROUP BY 1, 2, 3
  HAVING abs(sum(j.debit) + sum(j.credit)) > 0.01),
rent AS (
  SELECT tr.option AS property, max(j.date)::date AS last_rent
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  JOIN xero_metadata_xerotracking tr ON tr.id = j.tracking1_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND a.type = 'REVENUE' AND a.name ILIKE '%Rental Income%'
    AND j.credit < 0 AND j.date < :fy_end::date + 1
  GROUP BY 1)
SELECT d.code, d.name AS deposit_account, round(-d.balance, 2) AS deposit_held, d.last_movement,
       r.property, r.last_rent, :fy_end::date - r.last_rent AS days_since_rent
FROM dep d
LEFT JOIN LATERAL (
  SELECT property, last_rent FROM rent
  WHERE d.token IS NOT NULL AND upper(rent.property) LIKE '%' || upper(d.token) || '%'
  ORDER BY last_rent DESC LIMIT 1) r ON true
WHERE r.last_rent IS NULL OR r.last_rent < :fy_end::date - 90
ORDER BY deposit_held DESC
""",
    ),
    dict(
        code='BAL-04', category='BAL', severity='medium', expected='list', owner_action='MC',
        title='Director / shareholder loan movements without a corresponding expense',
        description='Journals (>= R1,000) in the FY that touch a loan / drawings / funds-introduced account and contain NO '
                    'P&L or fixed-asset line — i.e. loan vs bank or loan vs another balance-sheet account. These are the '
                    '"loan-without-expense" movements (R320k ambiguous): real loans, or reimbursements of expenses that '
                    'were never captured? MC decides.',
        rationale='Allocation audit 2026-08-17: loan-without-expense pattern.',
        sql_text="""
WITH loans AS (
  SELECT account_id FROM xero_metadata_xeroaccount
  WHERE organisation_id = :tenant_id
    AND type IN ('LIABILITY', 'CURRLIAB', 'TERMLIAB', 'NONCURRENT')
    AND (name ILIKE 'Loan%' OR name ILIKE '%Drawings%' OR name ILIKE '%Funds Introduced%')),
jl AS (
  SELECT j.journal_type, j.journal_number, j.date::date AS jdate, j.description, j.reference, j.debit, j.credit,
         j.account_id, a.name AS account, a.type AS acct_type
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type IN ('transaction', 'manual_journal')
    AND j.date >= :fy_start AND j.date < :fy_end::date + 1),
hit AS (SELECT DISTINCT journal_type, journal_number FROM jl WHERE account_id IN (SELECT account_id FROM loans))
SELECT jl.journal_type, jl.journal_number, min(jl.jdate) AS jdate,
       left(max(jl.description), 120) AS description, max(jl.reference) AS reference,
       round(sum(jl.debit), 2) AS journal_total,
       string_agg(DISTINCT jl.account, ' | ') AS accounts
FROM hit
JOIN jl USING (journal_type, journal_number)
GROUP BY 1, 2
HAVING bool_and(jl.acct_type NOT IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED','REVENUE','OTHERINCOME'))
   AND sum(jl.debit) >= 1000
ORDER BY journal_total DESC
""",
    ),
    dict(
        code='BAL-05', category='BAL', severity='medium', expected='list', owner_action='accountant',
        title='Accrued / suspense / control balances older than 12 months',
        description='Current-asset / current-liability / liability accounts (excluding AP, AR, VAT, tax, deposits, loans, '
                    'mortgages, bank control) with a non-zero balance at FY end and no movement in the 12 months before '
                    'FY end. Stale accruals, suspense and control balances.',
        rationale='Balance-sheet lifecycle.',
        sql_text="""
SELECT a.code, a.name, a.type,
       round(sum(j.debit) + sum(j.credit), 2) AS balance_at_fy_end,
       max(j.date)::date AS last_movement,
       :fy_end::date - max(j.date)::date AS days_stale
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
WHERE j.organisation_id = :tenant_id
  AND j.journal_type <> 'journal'
  AND j.date < :fy_end::date + 1
  AND a.type IN ('CURRLIAB', 'CURRENT', 'LIABILITY')
  AND a.name NOT ILIKE 'Deposit%'
  AND a.name NOT ILIKE 'Loan%'
  AND a.name !~* '(accounts payable|accounts receivable|^vat$|mortgage|tax|bank control|cash$)'
GROUP BY 1, 2, 3
HAVING abs(sum(j.debit) + sum(j.credit)) > 0.01
   AND max(j.date)::date < :fy_end::date - 365
ORDER BY abs(sum(j.debit) + sum(j.credit)) DESC
""",
    ),
    dict(
        code='BAL-06', category='BAL', severity='medium', expected='list', owner_action='bookkeeper',
        title='Aged payables > 90 days',
        description='Live ACCPAY bills with amount_due > 0 dated more than 90 days before FY end (as at the mirror date — '
                    'bills settled after the mirror refresh drop out). Unpaid or mis-allocated. Uses the invoice mirror '
                    'because xero_data_agedpayable is empty (RDY-05).',
        rationale='Balance-sheet lifecycle.',
        sql_text="""
SELECT i.contact_name AS supplier, i.invoice_number, i.date AS bill_date, i.due_date,
       i.total, i.amount_due, i.status,
       :fy_end::date - i.date AS age_days_at_fy_end
FROM xero_data_xeroinvoice i
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
  AND i.status = 'AUTHORISED' AND coalesce(i.amount_due, 0) > 0
  AND i.date <= :fy_end::date - 90
ORDER BY i.amount_due DESC
""",
    ),
    dict(
        code='BAL-07', category='BAL', severity='medium', expected='list', owner_action='MC',
        title='Aged receivables > 90 days',
        description='Live ACCREC invoices with amount_due > 0 dated more than 90 days before FY end (as at the mirror '
                    'date). Uncollected income → chase or provide for bad debt.',
        rationale='Balance-sheet lifecycle.',
        sql_text="""
SELECT i.contact_name AS customer, i.invoice_number, i.date AS invoice_date, i.due_date,
       i.total, i.amount_due, i.status,
       :fy_end::date - i.date AS age_days_at_fy_end
FROM xero_data_xeroinvoice i
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCREC'
  AND i.status = 'AUTHORISED' AND coalesce(i.amount_due, 0) > 0
  AND i.date <= :fy_end::date - 90
ORDER BY i.amount_due DESC
""",
    ),

    # ----------------------------------------------------------------- F. ALC
    dict(
        code='ALC-01', category='ALC', severity='medium', expected='list', owner_action='bookkeeper',
        title='Supplier allocated outside its dominant account (>= 70% rule)',
        description='For suppliers with >= 5 debit lines over the FY and prior FY: if one account carries >= 70% of the '
                    'spend, list the FY lines posted to any other account. Misallocation candidates.',
        rationale='Allocation audit 2026-08-17.',
        sql_text="""
WITH l AS (
  SELECT j.contact_id, j.account_id, j.debit, j.journal_number, j.date::date AS jdate, j.description
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND j.contact_id IS NOT NULL AND j.debit > 0
    AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED')
    AND j.date >= :fy_start::date - 365 AND j.date < :fy_end::date + 1),
per AS (SELECT contact_id, account_id, count(*) AS n, sum(debit) AS amt FROM l GROUP BY 1, 2),
tot AS (SELECT contact_id, count(*) AS n, sum(debit) AS amt FROM l GROUP BY 1),
dom AS (
  SELECT DISTINCT ON (p.contact_id) p.contact_id, p.account_id AS dom_account, p.amt / t.amt AS share
  FROM per p JOIN tot t USING (contact_id)
  WHERE t.n >= 5 AND t.amt > 0
  ORDER BY p.contact_id, p.amt DESC)
SELECT c.name AS supplier, da.name AS dominant_account, round(100 * d.share) AS dominant_pct,
       l.journal_number, l.jdate, left(l.description, 100) AS description, l.debit, a.name AS posted_to
FROM l
JOIN dom d ON d.contact_id = l.contact_id AND d.share >= 0.7 AND l.account_id <> d.dom_account
JOIN xero_metadata_xeroaccount a ON a.account_id = l.account_id
JOIN xero_metadata_xeroaccount da ON da.account_id = d.dom_account
JOIN xero_metadata_xerocontacts c ON c.contacts_id = l.contact_id
WHERE l.jdate BETWEEN :fy_start AND :fy_end
ORDER BY supplier, l.debit DESC
""",
    ),
    dict(
        code='ALC-02', category='ALC', severity='medium', expected='list', owner_action='bookkeeper',
        title='Description contradicts account (keyword heuristics)',
        description='Debit lines in the FY whose description/contact matches a keyword family (fuel, meals, groceries, '
                    'electronics retail, hardware, software) but whose account name does not match the accounts that '
                    'family normally belongs to. E.g. fuel → Sound Equipment.',
        rationale='Allocation audit 2026-08-17.',
        sql_text=r"""
WITH rules(rule, keyword_regex, expected_account_regex) AS (VALUES
  ('fuel keyword',        '(petrol|diesel|\mfuel\M(?!\s*levy)|\mengen\M|\mshell\M|\msasol\M|\mcaltex\M|\mastron\M|total energies|\mbp\M)',
                          '(fuel|motor vehicle|travel|transport)'),
  ('meal keyword',        '(uber eats|restaurant|coffee|\mcafe\M|\mkfc\M|nando|\mspur\M|steers|mcdonald|wimpy|burger|pizza|sushi)',
                          '(entertainment|staff meals|restaurant|dining|travel|guest)'),
  ('grocery keyword',     '(woolworths|checkers|\mspar\M|pick n pay|kwikspar|food lover)',
                          '(grocer|entertainment|staff meals|consumable|guest|food|travel)'),
  ('electronics keyword', '(takealot|incredible connection|\mmakro\M|game stores|hifi corp|istore|apple store)',
                          '(equipment|computer|electrical|furniture|asset|office|consumable|software|<7000|< r7000)'),
  ('hardware keyword',    '(builders warehouse|cashbuild|eezibuild|\mbuco\M|chamberlain|\mmica\M|leroy merlin)',
                          '(maintenance|repair|improvement|building|contractor|garden|renovation|consumable|equipment|asset)'),
  ('software keyword',    '(netflix|spotify|apple\.com|google \*|microsoft|adobe|openai|anthropic|\mxero\M|dropbox|zoom\.us|github)',
                          '(software|subscription|office expense|marketing|loan|personal)')
)
SELECT r.rule, j.journal_number, j.date::date AS jdate, c.name AS contact,
       left(j.description, 100) AS description, j.debit, a.name AS account
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
LEFT JOIN xero_metadata_xerocontacts c ON c.contacts_id = j.contact_id
JOIN rules r ON (coalesce(j.description, '') || ' ' || coalesce(c.name, '')) ~* r.keyword_regex
            AND a.name !~* r.expected_account_regex
WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
  AND j.debit > 0
  AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED')
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
ORDER BY r.rule, j.debit DESC
""",
    ),
    dict(
        code='ALC-03', category='ALC', severity='medium', expected='list', owner_action='accountant',
        title='Repair-vs-improvement consistency per contractor',
        description='Contractors with FY spend both expensed (repair / maintenance / contractor / renovation accounts) and '
                    'capitalised (FIXED asset accounts). DME R77k expensed vs R745k capitalised — the accountant decides '
                    'the split (s11(d) repair vs capital improvement / s13sex).',
        rationale='Allocation audit 2026-08-17: DME repair-vs-improvement.',
        sql_text="""
WITH l AS (
  SELECT j.contact_id, a.type, a.name AS account, j.debit
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND j.contact_id IS NOT NULL AND j.debit > 0
    AND j.date >= :fy_start AND j.date < :fy_end::date + 1
    AND (a.type = 'FIXED'
         OR (a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS')
             AND a.name ~* '(repair|maintenance|contractor|renovation|improvement|builder|handyman|plumb|electric|paint)')))
SELECT c.name AS contractor,
       round(sum(debit) FILTER (WHERE type <> 'FIXED'), 2) AS expensed,
       round(sum(debit) FILTER (WHERE type = 'FIXED'), 2) AS capitalised,
       string_agg(DISTINCT account, ' | ') AS accounts
FROM l
JOIN xero_metadata_xerocontacts c ON c.contacts_id = l.contact_id
GROUP BY 1
HAVING sum(debit) FILTER (WHERE type <> 'FIXED') > 0 AND sum(debit) FILTER (WHERE type = 'FIXED') > 0
ORDER BY coalesce(sum(debit) FILTER (WHERE type <> 'FIXED'), 0) + coalesce(sum(debit) FILTER (WHERE type = 'FIXED'), 0) DESC
""",
    ),
    dict(
        code='ALC-04', category='ALC', severity='medium', expected='list', owner_action='accountant',
        title='Asset threshold: items >= R7,000 in "<7000" accounts; items < R1,000 capitalised',
        description='Debit lines in the FY either >= R7,000 posted to an "Equipment < 7000 / Assets < R7000" expense account '
                    '(should be capitalised), or < R1,000 posted to a FIXED "@ Cost" account (should be expensed).',
        rationale='Allocation audit 2026-08-17.',
        sql_text="""
SELECT CASE WHEN a.type = 'FIXED' THEN 'small item capitalised (< R1,000)'
            ELSE 'large item in <7000 account (>= R7,000)' END AS issue,
       j.journal_number, j.date::date AS jdate, c.name AS contact,
       left(j.description, 100) AS description, j.debit, a.name AS account
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
LEFT JOIN xero_metadata_xerocontacts c ON c.contacts_id = j.contact_id
WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
  AND j.debit > 0
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  AND ((a.type <> 'FIXED' AND a.name ~* '(< ?r?7 ?000|<7000)' AND j.debit >= 7000)
       OR (a.type = 'FIXED' AND a.name ILIKE '%@ cost%' AND j.debit < 1000))
ORDER BY issue, j.debit DESC
""",
    ),
    dict(
        code='ALC-05', category='ALC', severity='high', expected='zero_rows', owner_action='bookkeeper',
        title='Input VAT claimed on entertainment',
        description='Lines in the FY on Entertainment / Staff Meals accounts carrying a non-zero tax amount. '
                    'VAT Act s17(2)(a) denies input VAT on entertainment.',
        rationale='Tax exposure.',
        sql_text="""
SELECT j.journal_number, j.date::date AS jdate, c.name AS contact, left(j.description, 100) AS description,
       j.debit, j.tax_amount, a.name AS account
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
LEFT JOIN xero_metadata_xerocontacts c ON c.contacts_id = j.contact_id
WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  AND a.name ~* '(entertainment|staff meals)'
  AND abs(coalesce(j.tax_amount, 0)) > 0
ORDER BY abs(j.tax_amount) DESC
""",
    ),
    dict(
        code='ALC-06', category='ALC', severity='medium', expected='list', owner_action='bookkeeper',
        title='Vatable supplier captured VAT-inclusive with no VAT split',
        description='Suppliers that have >= 3 lines with VAT in the last ~2 FYs (so they are VAT vendors), listed where FY '
                    'debit lines >= R100 on deductible expense/asset accounts carry zero tax. Unclaimed input VAT.',
        rationale='Tax — unclaimed input VAT.',
        sql_text="""
WITH vatable AS (
  SELECT contact_id
  FROM xero_data_xerojournals
  WHERE organisation_id = :tenant_id AND journal_type = 'transaction'
    AND contact_id IS NOT NULL AND abs(coalesce(tax_amount, 0)) > 0
    AND date >= :fy_start::date - 365
  GROUP BY 1 HAVING count(*) >= 3)
SELECT c.name AS supplier, j.journal_number, j.date::date AS jdate, left(j.description, 100) AS description,
       j.debit, a.name AS account
FROM xero_data_xerojournals j
JOIN vatable v ON v.contact_id = j.contact_id
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
JOIN xero_metadata_xerocontacts c ON c.contacts_id = j.contact_id
WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
  AND j.debit >= 100 AND coalesce(j.tax_amount, 0) = 0
  AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS','FIXED')
  AND a.name !~* '(non-deduct|loan|personal|entertainment|staff meals|salar|wages|interest|bank fee|insurance|rates|municipal|uif|sdl|donation|fines|penalt|depreciation|impairment|management fee)'
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
ORDER BY supplier, j.debit DESC
""",
    ),
    dict(
        code='ALC-07', category='ALC', severity='high', expected='zero_rows', owner_action='bookkeeper',
        title='Income credited into expense accounts',
        description='Credit lines on expense accounts in the FY that originate from money received (bank RECEIVE >= R2,000 '
                    'or any amount mentioning rent) or from a sales invoice (ACCREC). R40k/mo rental income in Rental Expense.',
        rationale='Allocation audit 2026-08-17.',
        sql_text="""
SELECT j.journal_number, j.date::date AS jdate, c.name AS contact, left(j.description, 100) AS description,
       j.credit, a.name AS account,
       s.transaction_source || ' ' || coalesce(s.collection->>'Type', '') AS source_kind
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
JOIN xero_data_xerotransactionsource s ON s.transactions_id = j.transaction_source_id
LEFT JOIN xero_metadata_xerocontacts c ON c.contacts_id = j.contact_id
WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  AND j.credit < 0
  AND a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS')
  AND ((s.transaction_source = 'BankTransaction' AND s.collection->>'Type' = 'RECEIVE'
        AND (j.credit <= -2000 OR j.description ~* 'rent'))
       OR (s.transaction_source = 'Invoice' AND s.collection->>'Type' = 'ACCREC'))
ORDER BY j.credit
""",
    ),
    dict(
        code='ALC-08', category='ALC', severity='high', expected='zero_rows', owner_action='bookkeeper',
        title='Future-dated journals',
        description='Any journal line for the tenant dated after today (e.g. the 2029-09-22 pair). Independent of FY.',
        rationale='Allocation audit 2026-08-17.',
        sql_text="""
SELECT journal_type, journal_number, date::date AS jdate, left(description, 100) AS description,
       reference, debit, credit
FROM xero_data_xerojournals
WHERE organisation_id = :tenant_id
  AND date::date > current_date
ORDER BY date DESC, journal_number
""",
    ),
    dict(
        code='ALC-09', category='ALC', severity='low', expected='list', owner_action='MC',
        title='Loan-vs-expense routing inconsistency per supplier',
        description='Suppliers posted in the FY both to a shareholder Loan account and to an expense account (e.g. the '
                    'same restaurant as personal loan one month and business entertainment the next). Policy decision.',
        rationale='Allocation audit 2026-08-17.',
        sql_text="""
WITH l AS (
  SELECT j.contact_id, to_char(j.date, 'YYYY-MM') AS ym,
         CASE WHEN a.name ILIKE 'Loan%' THEN 'loan' ELSE 'expense' END AS route,
         j.debit
  FROM xero_data_xerojournals j
  JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
  WHERE j.organisation_id = :tenant_id AND j.journal_type = 'transaction'
    AND j.contact_id IS NOT NULL AND j.debit > 0
    AND j.date >= :fy_start AND j.date < :fy_end::date + 1
    AND (a.name ILIKE 'Loan%' OR a.type IN ('EXPENSE','OVERHEADS','DIRECTCOSTS')))
SELECT c.name AS supplier,
       count(*) FILTER (WHERE route = 'loan') AS loan_lines,
       round(sum(debit) FILTER (WHERE route = 'loan'), 2) AS loan_total,
       count(DISTINCT ym) FILTER (WHERE route = 'loan') AS loan_months,
       count(*) FILTER (WHERE route = 'expense') AS expense_lines,
       round(sum(debit) FILTER (WHERE route = 'expense'), 2) AS expense_total,
       count(DISTINCT ym) FILTER (WHERE route = 'expense') AS expense_months
FROM l
JOIN xero_metadata_xerocontacts c ON c.contacts_id = l.contact_id
GROUP BY 1
HAVING count(*) FILTER (WHERE route = 'loan') > 0 AND count(*) FILTER (WHERE route = 'expense') > 0
ORDER BY coalesce(sum(debit) FILTER (WHERE route = 'loan'), 0) + coalesce(sum(debit) FILTER (WHERE route = 'expense'), 0) DESC
""",
    ),
    dict(
        code='ALC-10', category='ALC', severity='medium', expected='list', owner_action='accountant',
        title='Round-amount manual journals >= R5k on bond / shareholder loan accounts',
        description='Manual-journal lines in the FY of a round thousand >= R5,000 on loan, bond, mortgage, drawings or '
                    'funds-introduced accounts. Deemed-dividend (s64E) surface — accountant reviews.',
        rationale='Allocation audit 2026-08-17: s64E surface.',
        sql_text="""
SELECT j.journal_number, j.date::date AS jdate, left(j.description, 100) AS description, j.reference,
       j.debit, j.credit, a.name AS account
FROM xero_data_xerojournals j
JOIN xero_metadata_xeroaccount a ON a.account_id = j.account_id
WHERE j.organisation_id = :tenant_id AND j.journal_type = 'manual_journal'
  AND j.date >= :fy_start AND j.date < :fy_end::date + 1
  AND abs(j.debit + j.credit) >= 5000
  AND mod(abs(j.debit + j.credit), 1000) = 0
  AND a.name ~* '(loan|bond|mortgage|drawings|funds introduced)'
ORDER BY abs(j.debit + j.credit) DESC
""",
    ),

    # ----------------------------------------------------------------- G. PRC
    dict(
        code='PRC-01', category='PRC', severity='low', expected='list', owner_action='MC',
        title='Recurring suppliers with < 50% of bills carrying a document (auto-forward rule needed) — proxy',
        description='GAP: mailbox data not in Postgres. Proxy: suppliers with >= 4 ACCPAY bills in the FY where fewer than '
                    '50% of bills carry a Xero attachment — candidates for a Gmail auto-forward rule to accounts@.',
        rationale='Process / intake.',
        sql_text="""
SELECT i.contact_name AS supplier, count(*) AS bills,
       count(*) FILTER (WHERE coalesce(i.has_attachments, false)) AS with_doc,
       round(100.0 * count(*) FILTER (WHERE coalesce(i.has_attachments, false)) / count(*), 0) AS coverage_pct,
       round(sum(i.total), 2) AS total
FROM xero_data_xeroinvoice i
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
  AND i.status IN ('PAID', 'AUTHORISED')
  AND i.date BETWEEN :fy_start AND :fy_end
GROUP BY 1
HAVING count(*) >= 4
   AND 100.0 * count(*) FILTER (WHERE coalesce(i.has_attachments, false)) / count(*) < 50
ORDER BY total DESC
""",
    ),
    dict(
        code='PRC-02', category='PRC', severity='high', expected='list', owner_action='MC',
        title='"Bank details changed" signals (invoice text / beneficiary duplicates)',
        description='Two fraud signals: (1) FY bill line descriptions or references mentioning new / changed bank(ing) '
                    'details; (2) Investec beneficiaries with the same normalised name but different account numbers. '
                    'GAP: mailbox text is not searchable yet — verify every hit by phone on a known number (WeCare Aug 2026).',
        rationale='WeCare bank-details fraud attempt, Aug 2026.',
        sql_text="""
SELECT 'xero bill text' AS signal, i.contact_name AS supplier, i.invoice_number AS ref, i.date AS doc_date,
       left(coalesce(li.description, i.reference), 200) AS evidence
FROM xero_data_xeroinvoice i
LEFT JOIN xero_data_xeroinvoicelineitem li ON li.invoice_id = i.id
WHERE i.organisation_id = :tenant_id AND i.type = 'ACCPAY'
  AND i.date BETWEEN :fy_start AND :fy_end
  AND (coalesce(li.description, '') ~* '(bank(ing)? details|new (bank )?account|account (number|details) (has |have )?changed|changed (our )?bank)'
       OR coalesce(i.reference, '') ~* '(bank(ing)? details|new (bank )?account)')
UNION ALL
SELECT 'investec beneficiary: same name, different account', b1.beneficiary_name,
       b1.account_number || ' vs ' || b2.account_number,
       greatest(b1.updated_at, b2.updated_at)::date,
       coalesce(b1.bank_name, '') || ' / ' || coalesce(b2.bank_name, '')
FROM investec_investecbeneficiary b1
JOIN investec_investecbeneficiary b2
  ON lower(regexp_replace(b1.beneficiary_name, '[^a-z0-9]', '', 'gi')) = lower(regexp_replace(b2.beneficiary_name, '[^a-z0-9]', '', 'gi'))
 AND b1.account_number <> b2.account_number AND b1.id < b2.id
ORDER BY 1, 4 DESC
""",
    ),
    dict(
        code='PRC-03', category='PRC', severity='medium', expected='list', owner_action='bookkeeper',
        title='Documents held with no Xero bill and no bank payment — proxy',
        description='GAP: mailbox invoices are not in Postgres. Proxy: slips in the register (FY) that are not in Xero AND '
                    'have no matching Investec debit (business or personal, same amount +-5 days) — a document we hold '
                    'for which nothing was booked and nothing was paid (MB INV-0055 pattern). Unpaid / unbooked.',
        rationale='Process / intake.',
        sql_text="""
SELECT s.slip_ts::date AS slip_date, s.slip_supplier, s.slip_total, s.slip_category, s.xero_status, s.filename
FROM whatsapp.v_slips_xero s
WHERE s.slip_ts >= :fy_start AND s.slip_ts < :fy_end::date + 1
  AND NOT coalesce(s.synced_to_xero, false)
  AND coalesce(s.xero_status, '') NOT ILIKE 'skipped%'
  AND s.slip_total ~ '^[0-9]+\\.?[0-9]*$'
  AND NOT EXISTS (
      SELECT 1 FROM investec_investecbanktransaction t
      JOIN investec_investecbankaccount a ON a.id = t.account_id
      WHERE a.account_name IN ('Klikk (Pty) Ltd', 'Mr MC Dippenaar')
        AND t.type = 'DEBIT'
        AND t.amount = s.slip_total::numeric
        AND t.transaction_date BETWEEN s.slip_ts::date - 5 AND s.slip_ts::date + 5)
ORDER BY s.slip_total::numeric DESC
""",
    ),
]

assert len(CHECKS) == 45, len(CHECKS)
assert len({c['code'] for c in CHECKS}) == 45
