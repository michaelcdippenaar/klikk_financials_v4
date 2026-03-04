#=====================================================================
# DRILL TRAIL BALANCE TO JOURNALS
#=====================================================================
# Drill-through process: from trail balance cube cell to source
# journal transactions. Uses ODBC datasource and queries v_xero_journal_drill.
#
# Parameters (from cube intersection - map in PAW drill definition):
#   pTenantId     - Organisation/tenant dimension element (Xero tenant_id)
#   pAccountCode  - Account dimension element (account code)
#   pYear         - Financial year dimension element (fin_year)
#   pMonth        - Financial period dimension element (fin_period 1-12)
#   pContactId    - Contact dimension element (optional)
#   pTracking1Id  - Tracking1 dimension element (optional)
#   pTracking2Id  - Tracking2 dimension element (optional)
#
# Datasource: ODBC, Klikk_Group_Planning_Production (Django PostgreSQL)
# View: v_xero_journal_drill
#
# Note: Set datasource type to "Relational (ODBC)" when creating the drill.
# This process builds DataSourceQuery in Prolog; TM1 executes it.
#=====================================================================

#****Begin: Prolog
# Build parameterized SQL - required params must be non-empty
vTenant = TRIM(pTenantId);
vAccount = TRIM(pAccountCode);
IF(vTenant @= '');
    ProcessError;
ENDIF;
IF(vAccount @= '');
    ProcessError;
ENDIF;
vYear = TRIM(pYear);
vMonth = TRIM(pMonth);
vContact = TRIM(pContactId);
vTrk1 = TRIM(pTracking1Id);
vTrk2 = TRIM(pTracking2Id);

# Base SELECT (includes fin_year, fin_period for PAW dimensions)
vSQL = 'SELECT id, journal_id, journal_number, journal_type, date, year, month, fin_year, fin_period, account_code, contact_name, tracking1_option, tracking2_option, description, reference, amount, debit, credit, tax_amount, transaction_source_type FROM v_xero_journal_drill WHERE 1=1';

# Add WHERE conditions for required params
vSQL = vSQL | ' AND tenant_id = ''' | vTenant | '''';
vSQL = vSQL | ' AND account_code = ''' | vAccount | '''';
IF(vYear @<> '');
    vSQL = vSQL | ' AND fin_year = ' | vYear;
ENDIF;
IF(vMonth @<> '');
    vSQL = vSQL | ' AND fin_period = ' | vMonth;
ENDIF;

# Optional filters (omit when param empty to include all for that dimension)
IF(vContact @<> '');
    vSQL = vSQL | ' AND contact_id::varchar = ''' | vContact | '''';
ENDIF;
IF(vTrk1 @<> '');
    vSQL = vSQL | ' AND tracking1_id::varchar = ''' | vTrk1 | '''';
ENDIF;
IF(vTrk2 @<> '');
    vSQL = vSQL | ' AND tracking2_id::varchar = ''' | vTrk2 | '''';
ENDIF;

vSQL = vSQL | ' ORDER BY date, journal_number, id';

# Configure datasource - TM1 uses these for the drill query (no ODBCConnect/ODBCQuery)
DataSourceType = 'ODBC';
DataSourceNameForServer = 'Klikk_Group_Planning_Production';
DataSourceNameForClient = 'Klikk_Group_Planning_Production';
DataSourceQuery = vSQL;
#****End: Prolog

#****Begin: Epilog
# Return result set to PAW for display (required for relational drill-through)
RETURNSQLTABLEHANDLE;
#****End: Epilog
