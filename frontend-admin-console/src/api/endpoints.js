import apiClient from './client';
import { API_ENDPOINTS, API_BASE_URL } from '../utils/constants';

/**
 * Get Xero API call statistics for rate limit tracking
 */
export async function getApiCallStats(tenantId = null) {
  const params = tenantId ? { tenant_id: tenantId } : {};
  const response = await apiClient.get(API_ENDPOINTS.API_CALL_STATS, { params });
  return response.data;
}

/**
 * Get Xero OAuth2 authorization URL
 */
export async function getXeroAuthUrl() {
  const response = await apiClient.get(API_ENDPOINTS.XERO_AUTH_INITIATE);
  return response.data;
}

/**
 * Get Xero connection status
 */
export async function getXeroConnectionStatus() {
  const response = await apiClient.get(API_ENDPOINTS.XERO_CONNECTION_STATUS);
  return response.data;
}

/**
 * Save Xero API credentials (client_id, client_secret)
 */
export async function saveXeroCredentials(clientId, clientSecret) {
  const response = await apiClient.post(API_ENDPOINTS.XERO_CREDENTIALS, {
    client_id: clientId,
    client_secret: clientSecret,
  });
  return response.data;
}

/**
 * Get list of tenants
 */
export async function getTenants() {
  const response = await apiClient.get(API_ENDPOINTS.TENANTS);
  return response.data;
}

/**
 * Update metadata (accounts, contacts, tracking)
 */
export async function updateMetadata(tenantId) {
  const response = await apiClient.post(API_ENDPOINTS.UPDATE_METADATA, {
    tenant_id: tenantId,
  });
  return response.data;
}

/**
 * Update data (transactions and journals)
 */
export async function updateData(tenantId, loadAll = false) {
  const response = await apiClient.post(API_ENDPOINTS.UPDATE_DATA, {
    tenant_id: tenantId,
    load_all: loadAll,
  });
  return response.data;
}

/**
 * Process journals (convert raw journals to line items)
 */
export async function processJournals(tenantId) {
  const response = await apiClient.post(API_ENDPOINTS.PROCESS_JOURNALS, {
    tenant_id: tenantId,
  });
  return response.data;
}

/**
 * Process trail balance
 */
export async function processTrailBalance(tenantId, options = {}) {
  const response = await apiClient.post(API_ENDPOINTS.PROCESS_CUBE, {
    tenant_id: tenantId,
    rebuild_trail_balance: options.rebuild_trail_balance || false,
    exclude_manual_journals: options.exclude_manual_journals || false,
  });
  return response.data;
}

/**
 * Get data summary
 */
export async function getSummary(tenantId) {
  const response = await apiClient.get(API_ENDPOINTS.SUMMARY, {
    params: { tenant_id: tenantId },
  });
  return response.data;
}

/**
 * Get trail balance with filters
 */
export async function getTrailBalance(tenantId, filters = {}) {
  const params = { tenant_id: tenantId, ...filters };
  const response = await apiClient.get(API_ENDPOINTS.TRAIL_BALANCE, { params });
  return response.data;
}

/**
 * Get line items with filters
 */
export async function getLineItems(tenantId, filters = {}) {
  const params = { tenant_id: tenantId, ...filters };
  const response = await apiClient.get(API_ENDPOINTS.LINE_ITEMS, { params });
  return response.data;
}

/**
 * Reconcile reports
 */
export async function reconcileReports(tenantId, params) {
  const queryParams = {
    tenant_id: tenantId,
    financial_year: params.financial_year,
    fiscal_year_start_month: params.fiscal_year_start_month || 7,
    tolerance: params.tolerance || 0.01,
  };
  const response = await apiClient.get(API_ENDPOINTS.RECONCILE, {
    params: queryParams,
  });
  return response.data;
}

/**
 * Get account balance summary (IS vs BS, in-balance / out-of-balance)
 */
export async function getAccountSummary(tenantId, filters = {}) {
  const response = await apiClient.get(API_ENDPOINTS.ACCOUNT_SUMMARY, {
    params: { tenant_id: tenantId, ...filters },
  });
  return response.data;
}

/**
 * Get P&L summary by tracking (Income / Expense / Total)
 */
export async function getPnlSummary(tenantId, filters = {}) {
  const params = { tenant_id: tenantId, ...filters };
  const response = await apiClient.get(API_ENDPOINTS.PNL_SUMMARY, { params });
  return response.data;
}

/**
 * Import P&L by tracking category from Xero
 */
export async function importPnlByTracking(tenantId, options = {}) {
  const response = await apiClient.post(API_ENDPOINTS.IMPORT_PNL_BY_TRACKING, {
    tenant_id: tenantId,
    from_date: options.from_date || null,
    to_date: options.to_date || null,
  });
  return response.data;
}

/**
 * Compare profit & loss
 */
export async function compareProfitLoss(tenantId, params) {
  const queryParams = { tenant_id: tenantId, ...params };
  const response = await apiClient.get(API_ENDPOINTS.COMPARE_PROFIT_LOSS, {
    params: queryParams,
  });
  return response.data;
}

/**
 * Validate balance sheet
 */
export async function validateBalanceSheet(tenantId, params) {
  const queryParams = { tenant_id: tenantId, ...params };
  const response = await apiClient.get(API_ENDPOINTS.BALANCE_SHEET, {
    params: queryParams,
  });
  return response.data;
}

// ---------------------------------------------------------------------------
// Investec
// ---------------------------------------------------------------------------

/**
 * Get Investec transactions (paginated, filterable)
 */
export async function getInvestecTransactions(params = {}) {
  const response = await apiClient.get(API_ENDPOINTS.INVESTEC_TRANSACTIONS, {
    params: {
      limit: params.limit ?? 100,
      offset: params.offset ?? 0,
      account_number: params.account_number || undefined,
      share_name: params.share_name || undefined,
      type: params.type || undefined,
    },
  });
  return response.data;
}

/**
 * Upload Investec transactions Excel file
 */
export async function uploadInvestecTransactions(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await apiClient.post(API_ENDPOINTS.INVESTEC_UPLOAD, formData, {
    headers: { 'Content-Type': null },
  });
  return response.data;
}

/**
 * Upload Investec portfolio Excel file(s)
 */
export async function uploadInvestecPortfolio(files) {
  const formData = new FormData();
  if (Array.isArray(files) && files.length > 1) {
    files.forEach((f) => formData.append('files', f));
  } else {
    const file = Array.isArray(files) ? files[0] : files;
    formData.append('file', file);
  }
  const response = await apiClient.post(API_ENDPOINTS.INVESTEC_PORTFOLIO_UPLOAD, formData, {
    headers: { 'Content-Type': null },
  });
  return response.data;
}

/**
 * Get Investec share name mappings (share_name, company, share_code)
 */
export async function getInvestecMappings() {
  const response = await apiClient.get(API_ENDPOINTS.INVESTEC_MAPPING);
  return response.data;
}

/**
 * Get share names that appear in transactions but are not in the mapping table
 */
export async function getInvestecUnmappedShareNames() {
  const response = await apiClient.get(API_ENDPOINTS.INVESTEC_UNMAPPED_SHARE_NAMES);
  return response.data;
}

/**
 * Upload Investec share name mapping Excel file
 */
export async function uploadInvestecMapping(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await apiClient.post(API_ENDPOINTS.INVESTEC_MAPPING_UPLOAD, formData, {
    headers: { 'Content-Type': null },
  });
  return response.data;
}

/**
 * Export share codes, names and company mapping to Excel (for edit and re-upload)
 */
export async function getInvestecExportMapping() {
  const response = await apiClient.get(API_ENDPOINTS.INVESTEC_EXPORT_MAPPING);
  return response.data;
}

/**
 * Export companies from portfolios
 */
export async function getInvestecExportCompanies() {
  const response = await apiClient.get(API_ENDPOINTS.INVESTEC_EXPORT_COMPANIES);
  return response.data;
}

/**
 * Export share names from transactions
 */
export async function getInvestecExportShareNames() {
  const response = await apiClient.get(API_ENDPOINTS.INVESTEC_EXPORT_SHARE_NAMES);
  return response.data;
}

/**
 * Export transactions to Excel (writes to server; returns filename for download)
 */
export async function getInvestecExportTransactions() {
  const response = await apiClient.get(API_ENDPOINTS.INVESTEC_EXPORT_TRANSACTIONS);
  return response.data;
}

/**
 * URL to download an exported Investec Excel file
 */
export function getInvestecExportDownloadUrl(filename) {
  return `${API_BASE_URL}/media/investec/exports/${filename}`;
}
