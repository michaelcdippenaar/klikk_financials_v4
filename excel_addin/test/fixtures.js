/* Server responses, shaped exactly as the pane consumes them. Small on
   purpose: two row dimensions, one column dimension, a total and its child —
   the smallest cube that can show the indentation bug. */
'use strict';

const FILTERS = {
  tenants: [{ tenant_id: 't1', tenant_name: 'Klikk (Pty) Ltd' }],
  journal_types: ['ACCREC', 'ACCPAY'],
  accounts: [{ code: '200', name: 'Sales' }],
  contacts: ['Acme']
};

const DIMENSIONS = {
  dimensions: [
    { key: 'account_class', label: 'Class' },
    { key: 'account', label: 'Account' },
    { key: 'fin_year', label: 'Financial year' }
  ],
  measures: [{ key: 'amount', label: 'Amount' }]
};

const CUBE = {
  measure: 'amount',
  measure_label: 'Amount',
  row_dims: [{ key: 'account_class', label: 'Class' }, { key: 'account', label: 'Account' }],
  col_dims: [{ key: 'fin_year', label: 'Financial year' }],
  cols: ['FY2026'],
  col_paths: [['FY2026']],
  rows: [
    { keys: ['REVENUE', null], cells: [100], depth: 0, is_total: true },
    { keys: [null, 'Sales'], cells: [100], depth: 1, is_total: false }
  ],
  col_totals: [100],
  grand_total: 100,
  leaf_count: 1,
  zero_rows: 0,
  truncated_rows: false,
  truncated_cols: false,
  spec: null
};

/* Every endpoint the pane touches on the connect-then-build path. Anything
   not listed answers 404, which is what the non-fatal paths expect. */
const ROUTES = {
  '/journals/filters/': FILTERS,
  '/journals/pivot/dimensions/': DIMENSIONS,
  '/journals/pivot/views/': { results: [] },
  '/journals/pivot/': CUBE
};

module.exports = { FILTERS, DIMENSIONS, CUBE, ROUTES };
