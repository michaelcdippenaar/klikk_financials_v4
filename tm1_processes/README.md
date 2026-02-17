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
