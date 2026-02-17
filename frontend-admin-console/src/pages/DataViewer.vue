<template>
  <q-page class="q-pa-md">
    <div class="text-h5 q-mb-md">Data Viewer</div>

    <div v-if="!dataStore.selectedTenant" class="q-pa-lg text-center">
      <q-icon name="info" size="3em" color="grey-5" />
      <div class="text-h6 q-mt-md text-grey-7">Please select a tenant first</div>
    </div>

    <div v-else>
      <q-tabs v-model="tab" class="text-grey" active-color="primary" indicator-color="primary">
        <q-tab name="summary" label="Summary" />
        <q-tab name="trail-balance" label="Trail Balance" />
        <q-tab name="pnl-summary" label="P&L Summary" />
        <q-tab name="line-items" label="Line Items" />
      </q-tabs>

      <q-tab-panels v-model="tab" animated>
        <!-- Summary Tab -->
        <q-tab-panel name="summary">
          <!-- Data counts -->
          <q-card class="q-mb-md">
            <q-card-section>
              <div class="text-h6">Data Summary</div>
            </q-card-section>
            <q-card-section>
              <q-btn
                label="Refresh All"
                color="primary"
                outline
                :loading="dataStore.loading"
                @click="refreshSummary"
                class="q-mb-md"
              />
              <div v-if="dataStore.summary" class="row q-gutter-md">
                <q-card class="col-12 col-sm-6 col-md-3">
                  <q-card-section>
                    <div class="text-grey-7 text-caption">Tenant</div>
                    <div class="text-h6">{{ dataStore.summary.tenant_name }}</div>
                  </q-card-section>
                </q-card>
                <q-card class="col-12 col-sm-6 col-md-3">
                  <q-card-section>
                    <div class="text-grey-7 text-caption">Accounts</div>
                    <div class="text-h6">{{ dataStore.summary.accounts_count }}</div>
                  </q-card-section>
                </q-card>
                <q-card class="col-12 col-sm-6 col-md-3">
                  <q-card-section>
                    <div class="text-grey-7 text-caption">Journals</div>
                    <div class="text-h6">{{ dataStore.summary.journals_count }}</div>
                  </q-card-section>
                </q-card>
                <q-card class="col-12 col-sm-6 col-md-3">
                  <q-card-section>
                    <div class="text-grey-7 text-caption">Trail Balance</div>
                    <div class="text-h6">{{ dataStore.summary.trail_balance_count }}</div>
                  </q-card-section>
                </q-card>
              </div>
            </q-card-section>
          </q-card>

          <!-- Diagnostics -->
          <q-card v-if="dataStore.accountSummary?.diagnostics" class="q-mb-md">
            <q-card-section>
              <div class="text-h6">Reconciliation Diagnostics</div>
              <div class="row q-gutter-md q-mt-sm">
                <div class="col-auto">
                  <span class="text-grey-7">Compared period: </span>
                  <strong>{{ dataStore.accountSummary.diagnostics.date_range }}</strong>
                  <span class="text-grey-7"> ({{ dataStore.accountSummary.diagnostics.xero_pnl_months }} months)</span>
                </div>
                <div class="col-auto">
                  <span class="text-grey-7">Xero P&L filtered to: </span>
                  <strong>{{ dataStore.accountSummary.diagnostics.xero_pnl_filtered_to }}</strong> tracking
                </div>
                <div class="col-auto">
                  <span class="text-grey-7">Tracking categories: </span>
                  <span>{{ (dataStore.accountSummary.diagnostics.tracking_categories || []).join(', ') }}</span>
                </div>
              </div>
              <q-banner dense class="q-mt-sm bg-light-blue-1 text-light-blue-10" rounded>
                <strong>Comparing:</strong> DB trail balance (from journals) vs Xero P&L report (unfiltered).
                ~88% of accounts match. Remaining differences are typically property-related accounts
                where multi-line transactions are not fully captured by the journal import.
              </q-banner>
            </q-card-section>
          </q-card>

          <!-- Account Summary Filters -->
          <q-card class="q-mb-md">
            <q-card-section>
              <div class="row items-center q-gutter-md">
                <div class="text-subtitle1 text-weight-medium">Filter by Period</div>
                <q-input
                  v-model="acctSummaryFilters.year"
                  label="Year"
                  type="number"
                  outlined
                  dense
                  clearable
                  style="max-width: 120px"
                />
                <q-input
                  v-model="acctSummaryFilters.month"
                  label="Month (1-12)"
                  type="number"
                  outlined
                  dense
                  clearable
                  style="max-width: 140px"
                  :rules="[v => !v || (v >= 1 && v <= 12) || '1-12']"
                />
                <q-btn
                  label="Load"
                  color="primary"
                  :loading="dataStore.loading"
                  @click="loadAccountSummary"
                />
                <q-btn
                  label="Clear"
                  color="grey"
                  flat
                  @click="acctSummaryFilters.year = null; acctSummaryFilters.month = null; loadAccountSummary()"
                />
              </div>
            </q-card-section>
          </q-card>

          <!-- Income Statement accounts -->
          <q-card v-if="dataStore.accountSummary" class="q-mb-md">
            <q-card-section>
              <div class="row items-center q-gutter-md">
                <div class="text-h6">Income Statement Accounts</div>
                <q-space />
                <q-badge color="positive" :label="`${dataStore.accountSummary.income_statement.in_balance} OK`" />
                <q-badge color="negative" :label="`${dataStore.accountSummary.income_statement.out_of_balance} DIFF`" />
                <q-badge color="grey" :label="`${dataStore.accountSummary.income_statement.no_xero_data} No Xero`" />
              </div>
            </q-card-section>
            <q-card-section>
              <q-table
                :rows="dataStore.accountSummary.income_statement.accounts"
                :columns="accountSummaryColumns"
                row-key="account_code"
                :filter="acctFilter"
                :pagination="{ rowsPerPage: 25 }"
                dense
                flat
                bordered
              >
                <template v-slot:top-right>
                  <q-input
                    v-model="acctFilter"
                    placeholder="Filter accounts..."
                    outlined
                    dense
                    clearable
                    style="min-width: 200px"
                  >
                    <template v-slot:prepend>
                      <q-icon name="filter_list" />
                    </template>
                  </q-input>
                </template>
                <template v-slot:body-cell-db_total="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">{{ formatAmount(props.value) }}</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-xero_total="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">
                      {{ props.value != null ? formatAmount(props.value) : '-' }}
                    </span>
                  </q-td>
                </template>
                <template v-slot:body-cell-diff="props">
                  <q-td :props="props">
                    <span v-if="props.value != null" :class="diffClass(props.value)" style="font-weight: 600">
                      {{ formatAmount(props.value) }}
                    </span>
                    <span v-else class="text-grey-5">-</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-in_balance="props">
                  <q-td :props="props">
                    <q-badge v-if="props.value === true" color="positive" label="OK" />
                    <q-badge v-else-if="props.value === false" color="negative" label="DIFF" />
                    <span v-else class="text-grey-5">-</span>
                  </q-td>
                </template>
              </q-table>
            </q-card-section>
          </q-card>

          <!-- Balance Sheet accounts -->
          <q-card v-if="dataStore.accountSummary">
            <q-card-section>
              <div class="row items-center q-gutter-md">
                <div class="text-h6">Balance Sheet Accounts</div>
                <q-space />
                <q-badge color="info" :label="`${dataStore.accountSummary.balance_sheet.total} accounts`" />
              </div>
            </q-card-section>
            <q-card-section>
              <q-table
                :rows="dataStore.accountSummary.balance_sheet.accounts"
                :columns="bsAccountColumns"
                row-key="account_code"
                :filter="bsFilter"
                :pagination="{ rowsPerPage: 25 }"
                dense
                flat
                bordered
              >
                <template v-slot:top-right>
                  <q-input
                    v-model="bsFilter"
                    placeholder="Filter accounts..."
                    outlined
                    dense
                    clearable
                    style="min-width: 200px"
                  >
                    <template v-slot:prepend>
                      <q-icon name="filter_list" />
                    </template>
                  </q-input>
                </template>
                <template v-slot:body-cell-db_total="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">{{ formatAmount(props.value) }}</span>
                  </q-td>
                </template>
              </q-table>
            </q-card-section>
          </q-card>
        </q-tab-panel>

        <!-- Trail Balance Tab -->
        <q-tab-panel name="trail-balance">
          <q-card>
            <q-card-section>
              <div class="text-h6">Trail Balance</div>
              <div class="row q-gutter-md q-mt-md items-end">
                <q-input
                  v-model="trailBalanceFilters.contact_name"
                  label="Search Contact"
                  outlined
                  dense
                  clearable
                  debounce="300"
                  style="min-width: 220px"
                >
                  <template v-slot:prepend>
                    <q-icon name="search" />
                  </template>
                </q-input>
                <q-input
                  v-model.number="trailBalanceFilters.year"
                  label="Year"
                  type="number"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-input
                  v-model.number="trailBalanceFilters.month"
                  label="Month"
                  type="number"
                  min="1"
                  max="12"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-input
                  v-model="trailBalanceFilters.account_id"
                  label="Account ID"
                  outlined
                  dense
                  style="min-width: 200px"
                />
                <q-input
                  v-model.number="trailBalanceFilters.limit"
                  label="Limit"
                  type="number"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-btn
                  label="Load"
                  color="primary"
                  :loading="dataStore.loading"
                  @click="loadTrailBalance"
                />
              </div>
              <div class="row items-center q-mt-sm q-gutter-md">
                <div v-if="trailBalanceData" class="text-caption text-grey-7">
                  Showing {{ trailBalanceData.count }} records
                </div>
                <q-space />
                <q-toggle
                  v-model="reconView"
                  label="Recon View (group by tracking)"
                  color="primary"
                  dense
                />
              </div>
            </q-card-section>
            <q-card-section>
              <!-- RECON VIEW: grouped by tracking+account+year+month -->
              <q-table
                v-if="reconView"
                :rows="reconRows"
                :columns="reconColumns"
                :loading="dataStore.loading"
                row-key="_key"
                :filter="tableFilter"
                :pagination="{ rowsPerPage: 50 }"
                dense
                flat
                bordered
              >
                <template v-slot:top-right>
                  <q-input
                    v-model="tableFilter"
                    placeholder="Filter results..."
                    outlined
                    dense
                    clearable
                    style="min-width: 200px"
                  >
                    <template v-slot:prepend>
                      <q-icon name="filter_list" />
                    </template>
                  </q-input>
                </template>
                <template v-slot:body-cell-db_total="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">
                      {{ formatAmount(props.value) }}
                    </span>
                  </q-td>
                </template>
                <template v-slot:body-cell-xero_pnl="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)" style="font-weight: 500">
                      {{ props.value != null ? formatAmount(props.value) : '-' }}
                    </span>
                  </q-td>
                </template>
                <template v-slot:body-cell-diff="props">
                  <q-td :props="props">
                    <span v-if="props.value != null" :class="diffClass(props.value)" style="font-weight: 600">
                      {{ formatAmount(props.value) }}
                    </span>
                    <span v-else class="text-grey-5">-</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-match="props">
                  <q-td :props="props">
                    <q-badge v-if="props.value === true" color="positive" label="OK" />
                    <q-badge v-else-if="props.value === false" color="negative" label="DIFF" />
                    <span v-else class="text-grey-5">-</span>
                  </q-td>
                </template>
              </q-table>

              <!-- DETAIL VIEW: individual rows -->
              <q-table
                v-else
                :rows="trailBalanceRows"
                :columns="trailBalanceColumns"
                :loading="dataStore.loading"
                row-key="id"
                :filter="tableFilter"
                :pagination="{ rowsPerPage: 50 }"
                dense
                flat
                bordered
              >
                <template v-slot:top-right>
                  <q-input
                    v-model="tableFilter"
                    placeholder="Filter results..."
                    outlined
                    dense
                    clearable
                    style="min-width: 200px"
                  >
                    <template v-slot:prepend>
                      <q-icon name="filter_list" />
                    </template>
                  </q-input>
                </template>
                <template v-slot:body-cell-amount="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">
                      {{ formatAmount(props.value) }}
                    </span>
                  </q-td>
                </template>
                <template v-slot:body-cell-balance_to_date="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">
                      {{ props.value != null ? formatAmount(props.value) : '-' }}
                    </span>
                  </q-td>
                </template>
              </q-table>
            </q-card-section>
          </q-card>
        </q-tab-panel>

        <!-- P&L Summary Tab -->
        <q-tab-panel name="pnl-summary">
          <q-card>
            <q-card-section>
              <div class="text-h6">P&L Summary by Tracking</div>
              <div class="row q-gutter-md q-mt-md items-end">
                <q-input
                  v-model.number="pnlSummaryFilters.year"
                  label="Year"
                  type="number"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-input
                  v-model.number="pnlSummaryFilters.month"
                  label="Month"
                  type="number"
                  min="1"
                  max="12"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-btn
                  label="Load"
                  color="primary"
                  :loading="dataStore.loading"
                  @click="loadPnlSummary"
                />
              </div>
              <div v-if="pnlSummaryData" class="text-caption q-mt-sm text-grey-7">
                {{ pnlSummaryData.count }} tracking groups
              </div>
            </q-card-section>
            <q-card-section>
              <q-table
                :rows="pnlSummaryRows"
                :columns="pnlSummaryColumns"
                :loading="dataStore.loading"
                row-key="_key"
                :filter="pnlTableFilter"
                :pagination="{ rowsPerPage: 50 }"
                dense
                flat
                bordered
              >
                <template v-slot:top-right>
                  <q-input
                    v-model="pnlTableFilter"
                    placeholder="Filter..."
                    outlined
                    dense
                    clearable
                    style="min-width: 200px"
                  >
                    <template v-slot:prepend>
                      <q-icon name="filter_list" />
                    </template>
                  </q-input>
                </template>
                <template v-slot:body-cell-db_income="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">{{ formatAmount(props.value) }}</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-db_expense="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">{{ formatAmount(props.value) }}</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-db_pnl="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)" style="font-weight: 600">{{ formatAmount(props.value) }}</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-xero_income="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">{{ props.value != null ? formatAmount(props.value) : '-' }}</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-xero_expense="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">{{ props.value != null ? formatAmount(props.value) : '-' }}</span>
                  </q-td>
                </template>
                <template v-slot:body-cell-xero_pnl="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)" style="font-weight: 600">
                      {{ props.value != null ? formatAmount(props.value) : '-' }}
                    </span>
                  </q-td>
                </template>
                <template v-slot:body-cell-pnl_diff="props">
                  <q-td :props="props">
                    <span v-if="props.value != null" :class="diffClass(props.value)" style="font-weight: 600">
                      {{ formatAmount(props.value) }}
                    </span>
                    <span v-else class="text-grey-5">-</span>
                  </q-td>
                </template>
              </q-table>
            </q-card-section>
          </q-card>
        </q-tab-panel>

        <!-- Line Items Tab -->
        <q-tab-panel name="line-items">
          <q-card>
            <q-card-section>
              <div class="text-h6">Line Items</div>
              <div v-if="lineItemsData" class="text-caption q-mb-md">
                Showing {{ lineItemsData.count }} of {{ lineItemsData.total_count }} ({{ lineItemsData.remaining }} remaining)
              </div>
              <div class="row q-gutter-md q-mt-md">
                <q-input
                  v-model="lineItemsFilters.date_from"
                  label="Date From (YYYY-MM-DD)"
                  outlined
                  dense
                  style="min-width: 180px"
                />
                <q-input
                  v-model="lineItemsFilters.date_to"
                  label="Date To (YYYY-MM-DD)"
                  outlined
                  dense
                  style="min-width: 180px"
                />
                <q-input
                  v-model.number="lineItemsFilters.year"
                  label="Year"
                  type="number"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-input
                  v-model.number="lineItemsFilters.month"
                  label="Month"
                  type="number"
                  min="1"
                  max="12"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-input
                  v-model.number="lineItemsFilters.limit"
                  label="Limit"
                  type="number"
                  outlined
                  dense
                  style="min-width: 120px"
                />
                <q-btn
                  label="Load"
                  color="primary"
                  :loading="dataStore.loading"
                  @click="loadLineItems"
                />
              </div>
            </q-card-section>
            <q-card-section>
              <q-table
                :rows="lineItemsRows"
                :columns="lineItemsColumns"
                :loading="dataStore.loading"
                row-key="id"
                :pagination="{ rowsPerPage: 25 }"
                dense
                flat
                bordered
              >
                <template v-slot:body-cell-amount="props">
                  <q-td :props="props">
                    <span :class="amountClass(props.value)">
                      {{ formatAmount(props.value) }}
                    </span>
                  </q-td>
                </template>
              </q-table>
            </q-card-section>
          </q-card>
        </q-tab-panel>
      </q-tab-panels>
    </div>
  </q-page>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue';
import { useDataStore } from '../stores/data';

const dataStore = useDataStore();
const tab = ref('summary');
const tableFilter = ref('');
const pnlTableFilter = ref('');
const reconView = ref(false);
const acctFilter = ref('');
const bsFilter = ref('');
const acctSummaryFilters = reactive({
  year: null,
  month: null,
});

const trailBalanceFilters = reactive({
  year: null,
  month: null,
  account_id: '',
  contact_name: '',
  limit: 5000,
});

const pnlSummaryFilters = reactive({
  year: null,
  month: null,
});

const lineItemsFilters = reactive({
  date_from: '',
  date_to: '',
  year: null,
  month: null,
  limit: 5000,
});

// Account summary columns (IS accounts with recon)
const accountSummaryColumns = [
  { name: 'account_code', label: 'Code', field: 'account_code', align: 'left', sortable: true },
  { name: 'account_name', label: 'Account', field: 'account_name', align: 'left', sortable: true },
  { name: 'account_type', label: 'Type', field: 'account_type', align: 'left', sortable: true },
  { name: 'db_total', label: 'DB Total', field: 'db_total', align: 'right', sortable: true },
  { name: 'xero_total', label: 'Xero P&L Total', field: 'xero_total', align: 'right', sortable: true },
  { name: 'diff', label: 'Diff', field: 'diff', align: 'right', sortable: true },
  { name: 'in_balance', label: 'Status', field: 'in_balance', align: 'center', sortable: true },
];

// Balance sheet account columns (no Xero comparison)
const bsAccountColumns = [
  { name: 'account_code', label: 'Code', field: 'account_code', align: 'left', sortable: true },
  { name: 'account_name', label: 'Account', field: 'account_name', align: 'left', sortable: true },
  { name: 'account_type', label: 'Type', field: 'account_type', align: 'left', sortable: true },
  { name: 'db_total', label: 'DB Total', field: 'db_total', align: 'right', sortable: true },
];

// Detail view columns (individual rows per contact)
const trailBalanceColumns = [
  { name: 'year', label: 'Year', field: 'year', align: 'left', sortable: true },
  { name: 'month', label: 'Month', field: 'month', align: 'left', sortable: true },
  { name: 'account_code', label: 'Code', field: 'account_code', align: 'left', sortable: true },
  { name: 'account_name', label: 'Account', field: 'account_name', align: 'left', sortable: true },
  { name: 'contact_name', label: 'Contact', field: 'contact_name', align: 'left', sortable: true },
  { name: 'tracking1', label: 'Tracking 1', field: 'tracking1', align: 'left', sortable: true },
  { name: 'tracking2', label: 'Tracking 2', field: 'tracking2', align: 'left', sortable: true },
  { name: 'amount', label: 'Amount', field: 'amount', align: 'right', sortable: true },
  { name: 'balance_to_date', label: 'Balance to Date', field: 'balance_to_date', align: 'right', sortable: true },
];

// Recon view columns (grouped by tracking+account+year+month)
const reconColumns = [
  { name: 'year', label: 'Year', field: 'year', align: 'left', sortable: true },
  { name: 'month', label: 'Month', field: 'month', align: 'left', sortable: true },
  { name: 'account_code', label: 'Code', field: 'account_code', align: 'left', sortable: true },
  { name: 'account_name', label: 'Account', field: 'account_name', align: 'left', sortable: true },
  { name: 'tracking1', label: 'Tracking 1', field: 'tracking1', align: 'left', sortable: true },
  { name: 'db_total', label: 'DB Total', field: 'db_total', align: 'right', sortable: true },
  { name: 'xero_pnl', label: 'Xero P&L', field: 'xero_pnl', align: 'right', sortable: true },
  { name: 'diff', label: 'Diff', field: 'diff', align: 'right', sortable: true },
  { name: 'match', label: 'Match', field: 'match', align: 'center', sortable: true },
];

// P&L Summary columns
const pnlSummaryColumns = [
  { name: 'tracking1', label: 'Tracking', field: 'tracking1', align: 'left', sortable: true },
  { name: 'year', label: 'Year', field: 'year', align: 'left', sortable: true },
  { name: 'month', label: 'Month', field: 'month', align: 'left', sortable: true },
  { name: 'db_income', label: 'DB Income', field: 'db_income', align: 'right', sortable: true },
  { name: 'db_expense', label: 'DB Expense', field: 'db_expense', align: 'right', sortable: true },
  { name: 'db_pnl', label: 'DB P&L', field: 'db_pnl', align: 'right', sortable: true },
  { name: 'xero_income', label: 'Xero Income', field: 'xero_income', align: 'right', sortable: true },
  { name: 'xero_expense', label: 'Xero Expense', field: 'xero_expense', align: 'right', sortable: true },
  { name: 'xero_pnl', label: 'Xero P&L', field: 'xero_pnl', align: 'right', sortable: true },
  { name: 'pnl_diff', label: 'P&L Diff', field: 'pnl_diff', align: 'right', sortable: true },
];

const lineItemsColumns = [
  { name: 'date', label: 'Date', field: 'date', align: 'left', sortable: true },
  { name: 'account_code', label: 'Code', field: 'account_code', align: 'left', sortable: true },
  { name: 'account_name', label: 'Account', field: 'account_name', align: 'left', sortable: true },
  { name: 'contact_name', label: 'Contact', field: 'contact_name', align: 'left', sortable: true },
  { name: 'description', label: 'Description', field: 'description', align: 'left' },
  { name: 'amount', label: 'Amount', field: 'amount', align: 'right', sortable: true },
  { name: 'transaction_source_type', label: 'Source', field: 'transaction_source_type', align: 'left', sortable: true },
];

// Detail view: individual rows (no xero_pnl columns here)
const trailBalanceRows = computed(() => {
  return dataStore.trailBalance?.results || [];
});

// Recon view: group by tracking1 + account + year + month
// Compare DB sum vs Xero P&L at the same aggregation level
const reconRows = computed(() => {
  const results = dataStore.trailBalance?.results || [];
  if (!results.length) return [];

  // Aggregate by tracking1 + account_code + year + month
  const groups = {};
  for (const r of results) {
    const key = `${r.tracking1 || ''}|${r.account_code}|${r.year}|${r.month}`;
    if (!groups[key]) {
      groups[key] = {
        _key: key,
        year: r.year,
        month: r.month,
        account_code: r.account_code,
        account_name: r.account_name,
        tracking1: r.tracking1,
        db_total: 0,
        xero_pnl: r.xero_pnl != null ? Number(r.xero_pnl) : null,
      };
    }
    groups[key].db_total += Number(r.amount || 0);
    // If any row in the group has xero_pnl, use it (all share the same value)
    if (groups[key].xero_pnl === null && r.xero_pnl != null) {
      groups[key].xero_pnl = Number(r.xero_pnl);
    }
  }

  return Object.values(groups).map(g => {
    g.db_total = Math.round(g.db_total * 100) / 100;
    if (g.xero_pnl != null) {
      g.diff = Math.round((g.xero_pnl - g.db_total) * 100) / 100;
      g.match = Math.abs(g.diff) < 0.01;
    } else {
      g.diff = null;
      g.match = null;
    }
    return g;
  }).sort((a, b) => {
    // Sort by year, month, account_code, tracking1
    if (a.year !== b.year) return a.year - b.year;
    if (a.month !== b.month) return a.month - b.month;
    if (a.account_code !== b.account_code) return (a.account_code || '').localeCompare(b.account_code || '');
    return (a.tracking1 || '').localeCompare(b.tracking1 || '');
  });
});

// P&L Summary rows
const pnlSummaryRows = computed(() => {
  const results = dataStore.pnlSummary?.results || [];
  return results.map((r, i) => ({
    ...r,
    _key: `${r.tracking1 || 'none'}|${r.year}|${r.month}|${i}`,
  }));
});

const pnlSummaryData = computed(() => {
  return dataStore.pnlSummary;
});

const trailBalanceData = computed(() => {
  return dataStore.trailBalance;
});

const lineItemsRows = computed(() => {
  return dataStore.lineItems?.results || [];
});

const lineItemsData = computed(() => {
  return dataStore.lineItems;
});

function formatAmount(val) {
  if (val === null || val === undefined) return '';
  const num = Number(val);
  return num.toLocaleString('en-ZA', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function amountClass(val) {
  if (val === null || val === undefined) return '';
  return Number(val) < 0 ? 'text-negative' : '';
}

function diffClass(val) {
  if (val === null || val === undefined) return 'text-grey-5';
  const n = Number(val);
  if (Math.abs(n) < 0.01) return 'text-positive';  // match
  return 'text-negative';  // mismatch
}

async function refreshSummary() {
  await Promise.all([
    dataStore.fetchSummary(),
    loadAccountSummary(),
  ]);
}

async function loadAccountSummary() {
  const filters = {};
  if (acctSummaryFilters.year) filters.year = acctSummaryFilters.year;
  if (acctSummaryFilters.month) filters.month = acctSummaryFilters.month;
  await dataStore.fetchAccountSummary(filters);
}

async function loadTrailBalance() {
  const filters = {};
  if (trailBalanceFilters.year) filters.year = trailBalanceFilters.year;
  if (trailBalanceFilters.month) filters.month = trailBalanceFilters.month;
  if (trailBalanceFilters.account_id) filters.account_id = trailBalanceFilters.account_id;
  if (trailBalanceFilters.contact_name) filters.contact_name = trailBalanceFilters.contact_name;
  if (trailBalanceFilters.limit) filters.limit = trailBalanceFilters.limit;
  await dataStore.fetchTrailBalance(filters);
}

async function loadPnlSummary() {
  const filters = {};
  if (pnlSummaryFilters.year) filters.year = pnlSummaryFilters.year;
  if (pnlSummaryFilters.month) filters.month = pnlSummaryFilters.month;
  await dataStore.fetchPnlSummary(filters);
}

async function loadLineItems() {
  const filters = {};
  if (lineItemsFilters.date_from) filters.date_from = lineItemsFilters.date_from;
  if (lineItemsFilters.date_to) filters.date_to = lineItemsFilters.date_to;
  if (lineItemsFilters.year) filters.year = lineItemsFilters.year;
  if (lineItemsFilters.month) filters.month = lineItemsFilters.month;
  if (lineItemsFilters.limit) filters.limit = lineItemsFilters.limit;
  await dataStore.fetchLineItems(filters);
}

onMounted(async () => {
  if (dataStore.selectedTenant) {
    await Promise.all([
      dataStore.fetchSummary(),
      dataStore.fetchAccountSummary(),
    ]);
  }
});
</script>
