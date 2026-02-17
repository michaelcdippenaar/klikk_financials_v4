#=====================================================================
# DIM ACCOUNT UPDATE
#=====================================================================
# This process updates the account dimension and unwinds groupings
#=====================================================================

vDim='account';
vConsolidator = 'Groupings';

# Unwind groupings hierarchy
ExecuteProcess('}bedrock.hier.unwind','pDim',vDim,'pConsol',vConsolidator,'pRecursive',1);

# ProcessQuit is required for hierarchy to unwind properly
ProcessQuit;
