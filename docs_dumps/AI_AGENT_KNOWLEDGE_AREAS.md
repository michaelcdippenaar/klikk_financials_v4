# AI Agent – Knowledge Areas and Tools

The Klikk AI chatbot is configured to use three main knowledge areas. Populate the **vectorized corpus** (and optional pinned docs) so the agent can retrieve relevant material when answering.

---

## 1. IBM Cognos Planning Analytics (TM1/PA)

**In scope:** TI and Rule syntax, generating MDX queries, REST API endpoints, and all other PA material (study in detail).

**How to populate:**
- **Import TM1 Docs** (Setup → Import TM1 Docs): pulls cubes, dimensions, processes, TI code, and cube rules from your TM1 server into project documents and the default corpus.
- Add or pin **IBM Cognos PA manuals**, TI snippets, and API docs as System Documents in the project; assign them to the project’s default corpus and run **Vectorize** so they are searchable.

**Tools:** The agent can run live PA/TM1 API calls (`paw get`, `paw mdx`) and will use vectorized PA documentation when you ask about TI, rules, MDX, or API usage.

---

## 2. Accounting, Tax and Booking in South Africa

**In scope:** South African accounting standards, VAT, income tax, booking practices, and related compliance.

**How to populate:**
- Create System Documents (or upload/paste) with your SA accounting, tax and booking notes, SARS/VAT guidance, or internal policies.
- Assign them to the project’s **default corpus** (or a dedicated “Accounting SA” corpus if you create one) and run **Vectorize**.
- Optionally pin key docs (e.g. chart of accounts, VAT treatment) so they are always in context.

**RAG:** When the user asks about accounting, tax, VAT, South Africa, booking, etc., the agent will search the vectorized docs and use the returned excerpts in its answer.

---

## 3. Financial Modeling and Business Intelligence (CMA-level)

**In scope:** Chartered Management Accountant–level concepts: ratios, valuation, budgeting, forecasting, dashboards, KPIs, and BI best practices.

**How to populate:**
- Add System Documents with financial modeling guidelines, ratio definitions, BI/CMA-style material, and internal playbooks.
- Assign to the project’s default corpus (or a dedicated “Financial Modeling / BI” corpus) and run **Vectorize**.

**RAG:** Queries about financial models, CMA, business intelligence, KPIs, ratios, valuation, budgeting, or dashboards will trigger retrieval from this material.

---

## Keeping the vectorized model up to date

- **Glossary (accounts & contacts):** Use **Refresh glossary & re-vectorize** in Setup so the agent knows account names/purpose and Suppliers vs Customers from Xero.
- **After changing any docs:** Re-run **Vectorize** on the corpus (or use the management command `refresh_ai_glossary --vectorize` if you only changed glossary).
- **Multiple corpora:** You can create separate corpora (e.g. “PA Manual”, “Accounting SA”, “Financial Modeling”) and assign documents to each; the agent currently uses the project’s **default corpus** for RAG. To use more than one corpus, the project’s default corpus can contain docs from all three areas, or you can extend the app to search multiple corpora per project.
