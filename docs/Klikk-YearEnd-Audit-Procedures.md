# Klikk (Pty) Ltd — Year-End Audit Procedures & Check Registry
*Living document. Seed content for the `audit.checks` registry in the Klikk Financials Postgres. Each check = one row = one parameterised SQL. When MC reports a new issue, add a check here and in the registry.*
*Version 1 — 19 Aug 2026. Built from the FY2026 year-end work (slip reconciliation, allocation audit, supplier audit, Xero code audit) and the misses found along the way.*

---

## 0. Why this exists — the miss that taught us

On 19 Aug 2026 MC asked "was the MoneyBadgers deposit ever refunded?" — R9,813.33 booked correctly as an asset in Sep 2025, lease ended Jan 2026, never recovered, never flagged. Three separate audits had passed it, because each tested **transactions** (captured? allocated? duplicated?) and none tested **balance-sheet items over time**. Deposits, prepayments, director loans, retentions, and supplier credits are correct when booked and become wrong by *staying* after the event that should clear them.

**Principle 1:** every audit must contain lifecycle checks ("should this balance still exist?") as well as transaction checks.
**Principle 2:** checks live as executable rows in Postgres and are run by the app/MCP, not re-invented per session. Narrative lives here; execution lives in the registry.
**Principle 3:** findings go to MC and the bookkeeper/auditors. Claude never writes to Xero except on MC's explicit, specific instruction (logged in `audit.xero_writes`).

---

## 1. How a year-end audit runs (the procedure)

1. **Scope** — entity = Klikk (Pty) Ltd Xero tenant `41ebfa0e-012e-4ff1-82ba-a9a7585c536c`; FY = 1 Jul → 30 Jun; mirror must be fresh (§2 checks confirm).
2. **Evidence intake** — slips (WhatsApp Slippies register `whatsapp.klikk_slips`), invoices (Gmail accounts@ + mc@ sweep), bank (Investec mirror, all accounts incl. personal), Xero mirror (journals, bills, attachments).
3. **Run the registry** — every active check for the FY; store results per run.
4. **Triage** — FAIL/WARN items → Asana tasks (section "Financial Yearend") grouped by who acts: MC / bookkeeper / supplier / accountant.
5. **Supplier statements** — for every supplier with an ISSUE/QUERY, request a statement from 1 Jan of prior FY; reconcile statement ↔ bank ↔ Xero (the three-way tie).
6. **Close** — each task closes on evidence (bank receipt, Xero correction, statement); re-run the registry at the end; diff vs opening run.

---

## 2. The check registry (v1)

Columns: **Code · Title · What it catches · SQL sketch · Expected · Who acts · Born from**

### A. Data-readiness (run first; everything else is unreliable if these fail)
| Code | Title | What it catches | Sketch | Expected |
|---|---|---|---|---|
| RDY-01 | Xero journal mirror fresh | stale `xero_data_xerojournals` | `max(date)` per tenant ≥ today−2 | PASS |
| RDY-02 | Investec mirror fresh, all accounts | stale bank | `max(transaction_date)` per account ≥ today−3 | PASS |
| RDY-03 | Trial balance nets to zero per FY | dropped VAT legs, half journals (Aug 2026 bug) | sum(debit)−sum(credit) by FY | 0.00 |
| RDY-04 | Voided journals in mirror | double-ingested voids (Boschendal ×2 bug) | exact-duplicate journal sets | 0 |
| RDY-05 | Aged payables/receivables tables populated | empty `xero_data_agedpayable` (found 18 Aug) | count > 0 | PASS |
| RDY-06 | Attachment coverage known | `has_attachments` null rows | count null | 0 |
| RDY-07 | Xero connection healthy, no tenant `reauth_required` | dead token | status endpoint | PASS |

### B. Document completeness (slips & invoices ↔ books)
| Code | Title | What it catches | Expected |
|---|---|---|---|
| DOC-01 | Slips with no Xero match (non-meals) | uncaptured purchases | list → bookkeeper |
| DOC-02 | Slips with no Xero match (meals) | policy pile | list → MC policy decision |
| DOC-03 | Klikk expense lines with no slip AND no Xero attachment | voucher-less spend ≥ R1,000 | list |
| DOC-04 | Invoice emails in mc@ never forwarded to accounts@ | intake bypass | list per supplier |
| DOC-05 | Xero bills without attachment, by supplier | missing source docs | coverage % per supplier |
| DOC-06 | Slip group messages with no file in register | lost media | 0 |

### C. Bank ↔ books (both directions, all accounts)
| Code | Title | What it catches | Expected |
|---|---|---|---|
| BNK-01 | Klikk-account debits with no Xero journal ±5d same amount | unbooked business payments | 0 |
| BNK-02 | **Personal-account debits to known Klikk suppliers with no Klikk journal** | DME R90k, Aurras R64k, Expedition R24k pattern | list → MC classify B/P |
| BNK-03 | Klikk-account credits from suppliers (refunds) not in Xero | unbooked refunds | 0 |
| BNK-04 | Journals with no bank movement (non-accrual accounts) | phantom payments | list |

### D. Supplier integrity (the DME battery)
| Code | Title | What it catches | Expected |
|---|---|---|---|
| SUP-01 | Payment referencing a bill number that has no bill | "Payment made – Bill X" with no bill X | 0 |
| SUP-02 | Payment dated before its bill | DME 0605 pattern | list |
| SUP-03 | Bill with 2+ payments | double payment | list |
| SUP-04 | Same supplier, same amount, within 7 days, different journals | duplicate capture | list (verify live-vs-voided) |
| SUP-05 | **Payment allocated directly from bank with NO bill behind it** (MC, 19 Aug) | bank-spend coded straight to expense; bill never created so invoice can't be allocated later (MoneyBadgers 10 Dec / 4 Jan) | list per supplier → bookkeeper creates bills |
| SUP-06 | Supplier invoice-number reused / paid twice | INV-035 reuse, CW Plumbers #19 | 0 |
| SUP-07 | Supplier statement three-way tie (when statement held) | bank paid ≠ statement received ≠ Xero | diff = 0 |
| SUP-08 | Supplier name fragmentation | "Carry on"/"Carry On", 4×BP | merge list |

### E. Balance-sheet lifecycle (the MoneyBadgers lesson — NEW)
| Code | Title | What it catches | Expected |
|---|---|---|---|
| BAL-01 | **Deposits held by suppliers where supplier activity ended** | asset in "Deposit on …" + last payment > 90 days ago + no refund credit | list → recover |
| BAL-02 | Supplier prepayments / credits not consumed within 90 days | overpayments sitting | list |
| BAL-03 | Tenant deposits held vs active leases | deposits for departed tenants not refunded/forfeited | list |
| BAL-04 | Director/shareholder loan movements without corresponding expense | loan-without-expense pattern (R320k ambiguous) | list → MC |
| BAL-05 | Accrued/retention balances older than 12 months | stale accruals | list |
| BAL-06 | Aged payables > 90 days | unpaid or mis-allocated | list |
| BAL-07 | Aged receivables > 90 days | uncollected | list |

### F. Allocation & tax
| Code | Title | What it catches | Expected |
|---|---|---|---|
| ALC-01 | Supplier allocated outside its dominant account (≥70% rule) | misallocation | list |
| ALC-02 | Description contradicts account (fuel→Sound Equipment etc.) | keyword heuristics | list |
| ALC-03 | Repair-vs-improvement consistency per contractor | DME R77k expensed vs R745k capitalised | list → accountant |
| ALC-04 | Items ≥ R7,000 in "<7000" accounts; items < R1,000 capitalised | asset threshold | list |
| ALC-05 | Input VAT claimed on entertainment | s17(2) | 0 |
| ALC-06 | Vatable supplier, VAT-inclusive capture, no VAT split | unclaimed input VAT | list |
| ALC-07 | Income credited into expense accounts | R40k/mo rental income in Rental Expense | 0 |
| ALC-08 | Future-dated journals | 2029-09-22 pair | 0 |
| ALC-09 | Loan-vs-expense routing inconsistency per supplier | restaurants both ways same month | list → policy |
| ALC-10 | Round-amount manual journals ≥ R5k between bond/shareholder loans | s64E surface | list → accountant |

### G. Process / intake
| Code | Title | What it catches | Expected |
|---|---|---|---|
| PRC-01 | Recurring suppliers with < 50% of invoices reaching accounts@ | auto-forward rule needed | list |
| PRC-02 | Suppliers with "bank details changed" text in recent invoice | fraud flag (WeCare Aug 2026) | list → verify by phone |
| PRC-03 | Supplier invoices in mailbox with no Xero bill and no payment (e.g. MB INV-0055) | unpaid/unbooked | list |

---

## 3. How new checks get added (the loop MC wants)

1. MC raises an issue in conversation ("was X ever refunded?").
2. Claude confirms the facts from data, then writes the **generalised** check (not "MoneyBadgers deposit" but "any supplier deposit after relationship end"), assigns a code, severity, owner, and the SQL.
3. Row inserted into `audit.checks`; this file updated; memory note added.
4. Check runs in every future `run_yearend_audit`.

Rule of thumb: if a human had to *know something about the world* to spot it (a lease ended, a job finished, a contractor changed banks), encode the world-fact as data (last-activity dates, contract end dates, bank-detail fields) so the check doesn't need the human next time.

---

## 4. Where it lives (target architecture)

- **Postgres (klikk_financials_v4)**: `audit.checks` (registry + SQL), `audit.check_runs`, `audit.check_results`, `audit.xero_writes` (exists). Structured, versioned, diffable year over year.
- **MCP (klikk-financials server)**: `list_audit_checks`, `run_audit_check(code, fy)`, `run_yearend_audit(fy)`, `audit_history(code)`, `add_audit_check(...)`. Saying "audit for financial year end" → agent calls `run_yearend_audit`, then reasons over results.
- **This document**: the narrative (why each check exists, how to interpret, who acts). Mirrored into the repo and into a skill; optionally indexed into the RAG store for "why does check BAL-01 exist?" questions — but execution never depends on retrieval.
- **Not RAG for the checks themselves**: procedures are executed, not recalled; determinism and history beat similarity search here.
