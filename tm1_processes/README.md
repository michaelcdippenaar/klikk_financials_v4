# TM1 TurboIntegrator Processes

This directory contains IBM Planning Analytics/TM1 TurboIntegrator process files (.pro).

## Files

### dim_account_update.pro
Updates the account dimension and unwinds groupings hierarchy using the bedrock.hier.unwind process.

**Important:** `ProcessQuit` is required for the hierarchy to unwind properly.

### cub_listed_share_src_holdings_import.pro
Imports listed share holdings data from an ODBC data source.

**ODBC Configuration:**
- DSN: `Klikk_Group_Planning_Production`
- Table/View: `cub_listed_share_src_holdings_import`

### drill_trail_balance_to_journals.pro
Drill-through process: from a trail balance cube cell to the underlying source journal transactions. Enables users to click a value in IBM Planning Analytics Workspace (PAW) and see the Xero journal lines that make up that amount.

**Prerequisites:**
- ODBC DSN `Klikk_Group_Planning_Production` must point to the Django PostgreSQL database
- Database view `v_xero_journal_drill` must exist (created by Django migration `0006_create_journal_drill_view`)

**Parameters** (map from trail balance cube dimensions in PAW drill definition):
| Parameter     | Required | View Column   | Description                          |
|---------------|----------|---------------|--------------------------------------|
| pTenantId     | Yes      | tenant_id     | Xero tenant/organisation ID          |
| pAccountCode  | Yes      | account_code  | Account code                         |
| pYear         | Yes      | year          | Year (integer)                       |
| pMonth        | Yes      | month         | Month (1-12)                         |
| pContactId    | No       | contact_id    | Contact ID (omit for all contacts)   |
| pTracking1Id  | No       | tracking1_id  | Tracking1 option ID                  |
| pTracking2Id  | No       | tracking2_id  | Tracking2 option ID                  |

**v_xero_journal_drill view** (PostgreSQL, created by migrations 0006, 0007):
- Exposes journal line detail from `xero_data_xerojournals` with joins to account, contact, tracking
- Columns: tenant_id, account_id, account_code, year, month, fiscal_year_start_month, fin_year, fin_period, contact_id, contact_name, tracking1_id, tracking1_option, tracking2_id, tracking2_option, id, journal_id, journal_number, journal_type, date, description, reference, amount, tax_amount, transaction_source_type
- fin_year and fin_period use tenant's fiscal_year_start_month (from Xero Organisation, default July=7)

**PAW setup:**
1. Upload the process to TM1
2. Create a drill-through definition on the trail balance cube
3. Set datasource type: Relational (ODBC)
4. Select this process
5. Map cube dimension elements to process parameters (pTenantId, pAccountCode, pYear, pMonth, etc.)

**Limitations:**
- Dimension element values in TM1 must match view column values (e.g. account_code, tenant_id format)
- Empty/optional parameters: omit from WHERE clause to include all for that dimension
- If TM1 uses display names instead of IDs for contact/tracking, the view may need additional lookup columns or the TI process may need translation logic

**Troubleshooting ODBC Errors:**
1. Verify ODBC DSN is configured correctly on the TM1 server
2. Check database connectivity
3. Verify user permissions for the database
4. Ensure the table/view exists and is accessible

## Usage

These processes can be:
1. Uploaded to TM1 server via TM1 Architect or Planning Analytics Workspace
2. Scheduled to run automatically
3. Executed manually via API or UI

## Notes

- All processes end with `ProcessQuit` to ensure proper completion
- ODBC connections should be tested before production use
- Error handling is included for ODBC operations
