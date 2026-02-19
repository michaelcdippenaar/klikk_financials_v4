// API Configuration
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';

// API Endpoints
export const API_ENDPOINTS = {
  // Auth
  LOGIN: '/api/auth/login/',
  REFRESH: '/api/auth/refresh/',

  // Xero Auth
  XERO_AUTH_INITIATE: '/xero/auth/initiate/',
  XERO_CONNECTION_STATUS: '/xero/auth/status/',
  XERO_CREDENTIALS: '/xero/auth/credentials/',
  
  // Core
  TENANTS: '/xero/core/tenants/',
  
  // Metadata
  UPDATE_METADATA: '/xero/metadata/update/',
  
  // Data
  UPDATE_DATA: '/xero/data/update/journals/',
  PROCESS_JOURNALS: '/xero/data/process/journals/',
  
  // Cube
  PROCESS_CUBE: '/xero/cube/process/',
  SUMMARY: '/xero/cube/summary/',
  TRAIL_BALANCE: '/xero/cube/trail-balance/',
  LINE_ITEMS: '/xero/cube/line-items/',
  IMPORT_PNL_BY_TRACKING: '/xero/cube/import-pnl-by-tracking/',
  PNL_SUMMARY: '/xero/cube/pnl-summary/',
  ACCOUNT_SUMMARY: '/xero/cube/account-summary/',
  
  // Sync
  API_CALL_STATS: '/xero/sync/api-call-stats/',

  // Validation
  RECONCILE: '/xero/validation/reconcile/',
  COMPARE_PROFIT_LOSS: '/xero/validation/compare-profit-loss/',
  BALANCE_SHEET: '/xero/validation/balance-sheet/',

  // Investec
  INVESTEC_UPLOAD: '/api/investec/upload/',
  INVESTEC_TRANSACTIONS: '/api/investec/transactions/',
  INVESTEC_PORTFOLIO_UPLOAD: '/api/investec/portfolio/upload/',
  INVESTEC_MAPPING: '/api/investec/mapping/',
  INVESTEC_UNMAPPED_SHARE_NAMES: '/api/investec/mapping/unmapped-share-names/',
  INVESTEC_MAPPING_UPLOAD: '/api/investec/mapping/upload/',
  INVESTEC_EXPORT_MAPPING: '/api/investec/export/mapping/',
  INVESTEC_EXPORT_COMPANIES: '/api/investec/export/companies/',
  INVESTEC_EXPORT_SHARE_NAMES: '/api/investec/export/share-names/',
  INVESTEC_EXPORT_TRANSACTIONS: '/api/investec/export/transactions/',
};

// Storage Keys
export const STORAGE_KEYS = {
  TOKEN: 'auth_token',
  REFRESH_TOKEN: 'refresh_token',
  USER: 'user',
  SELECTED_TENANT: 'selected_tenant',
};
