<template>
  <q-page class="q-pa-md">
    <div class="text-h5 q-mb-md">Process Runner</div>

    <div v-if="!dataStore.selectedTenant" class="q-pa-lg text-center">
      <q-icon name="info" size="3em" color="grey-5" />
      <div class="text-h6 q-mt-md text-grey-7">Please select a tenant first</div>
    </div>

    <div v-else>
      <!-- Update Metadata -->
      <ProcessCard
        title="Update Metadata"
        description="Update accounts, contacts, and tracking categories from Xero"
        :loading="loading.metadata"
        :result="results.metadata"
        @run="runMetadata"
      />

      <!-- Update Data -->
      <ProcessCard
        title="Sync Transactions & Journals"
        description="Fetch and update bank transactions, invoices, payments, and journals from Xero"
        :loading="loading.data"
        :result="results.data"
        show-form
      >
        <template #form>
          <q-checkbox
            v-model="dataOptions.loadAll"
            label="Load all data (ignore last update timestamp)"
          />
        </template>
        <template #actions>
          <q-btn
            label="Sync Data"
            color="primary"
            :loading="loading.data"
            @click="runDataUpdate"
          />
        </template>
      </ProcessCard>

      <!-- Process Journals -->
      <ProcessCard
        title="Process Journals"
        description="Convert raw journal data to individual journal line items"
        :loading="loading.journals"
        :result="results.journals"
        @run="runProcessJournals"
      />

      <!-- Process Trail Balance -->
      <ProcessCard
        title="Build Trail Balance"
        description="Consolidate journals into trail balance records"
        :loading="loading.trailBalance"
        :result="results.trailBalance"
        show-form
      >
        <template #form>
          <q-checkbox
            v-model="trailBalanceOptions.rebuild"
            label="Rebuild trail balance (delete existing and rebuild)"
          />
          <q-checkbox
            v-model="trailBalanceOptions.excludeManual"
            label="Exclude manual journals"
            class="q-mt-sm"
          />
        </template>
        <template #actions>
          <q-btn
            label="Build Trail Balance"
            color="primary"
            :loading="loading.trailBalance"
            @click="runTrailBalance"
          />
        </template>
      </ProcessCard>

      <!-- Import P&L by Tracking -->
      <ProcessCard
        title="Import P&L by Tracking"
        description="Pull Xero P&L report for every tracking option and store per-account/month values for comparison"
        :loading="loading.pnlByTracking"
        :result="results.pnlByTracking"
        show-form
      >
        <template #form>
          <div class="row q-gutter-md">
            <q-input
              v-model="pnlTrackingOptions.fromDate"
              label="From Date (YYYY-MM-DD)"
              outlined
              dense
              style="min-width: 180px"
              hint="Default: 12 months ago"
            />
            <q-input
              v-model="pnlTrackingOptions.toDate"
              label="To Date (YYYY-MM-DD)"
              outlined
              dense
              style="min-width: 180px"
              hint="Default: today"
            />
          </div>
        </template>
        <template #actions>
          <q-btn
            label="Import P&L by Tracking"
            color="primary"
            :loading="loading.pnlByTracking"
            @click="runPnlByTracking"
          />
        </template>
      </ProcessCard>

      <!-- Reconcile Reports -->
      <ProcessCard
        title="Reconcile Reports"
        description="Compare Xero P&L and Balance Sheet reports to constructed trail balance"
        :loading="loading.reconcile"
        :result="results.reconcile"
        show-form
      >
        <template #form>
          <div class="row q-gutter-md">
            <q-input
              v-model.number="reconcileOptions.financialYear"
              label="Financial Year"
              type="number"
              outlined
              dense
              style="min-width: 150px"
            />
            <q-input
              v-model.number="reconcileOptions.fiscalYearStartMonth"
              label="Fiscal Year Start Month"
              type="number"
              min="1"
              max="12"
              outlined
              dense
              style="min-width: 150px"
            />
            <q-input
              v-model.number="reconcileOptions.tolerance"
              label="Tolerance"
              type="number"
              step="0.01"
              outlined
              dense
              style="min-width: 150px"
            />
          </div>
        </template>
        <template #actions>
          <q-btn
            label="Run Reconciliation"
            color="primary"
            :loading="loading.reconcile"
            @click="runReconcile"
          />
        </template>
      </ProcessCard>
    </div>
  </q-page>
</template>

<script setup>
import { ref, reactive } from 'vue';
import { useDataStore } from '../stores/data';
import { useProcessStore } from '../stores/processes';
import ProcessCard from '../components/ProcessCard.vue';

const dataStore = useDataStore();
const processStore = useProcessStore();

const loading = reactive({
  metadata: false,
  data: false,
  journals: false,
  trailBalance: false,
  pnlByTracking: false,
  reconcile: false,
});

const results = reactive({
  metadata: null,
  data: null,
  journals: null,
  trailBalance: null,
  pnlByTracking: null,
  reconcile: null,
});

const dataOptions = reactive({
  loadAll: false,
});

const trailBalanceOptions = reactive({
  rebuild: false,
  excludeManual: false,
});

const pnlTrackingOptions = reactive({
  fromDate: '',
  toDate: '',
});

const reconcileOptions = reactive({
  financialYear: new Date().getFullYear(),
  fiscalYearStartMonth: 7,
  tolerance: 0.01,
});

async function runMetadata() {
  loading.metadata = true;
  results.metadata = null;
  try {
    const result = await processStore.runProcess('metadata', {
      tenantId: dataStore.selectedTenant,
    });
    results.metadata = result;
  } finally {
    loading.metadata = false;
  }
}

async function runDataUpdate() {
  loading.data = true;
  results.data = null;
  try {
    const result = await processStore.runProcess('data', {
      tenantId: dataStore.selectedTenant,
      loadAll: dataOptions.loadAll,
    });
    results.data = result;
  } finally {
    loading.data = false;
  }
}

async function runProcessJournals() {
  loading.journals = true;
  results.journals = null;
  try {
    const result = await processStore.runProcess('journals', {
      tenantId: dataStore.selectedTenant,
    });
    results.journals = result;
  } finally {
    loading.journals = false;
  }
}

async function runTrailBalance() {
  loading.trailBalance = true;
  results.trailBalance = null;
  try {
    const result = await processStore.runProcess('trail-balance', {
      tenantId: dataStore.selectedTenant,
      rebuild_trail_balance: trailBalanceOptions.rebuild,
      exclude_manual_journals: trailBalanceOptions.excludeManual,
    });
    results.trailBalance = result;
    // Refresh summary after trail balance is built
    await dataStore.fetchSummary();
  } finally {
    loading.trailBalance = false;
  }
}

async function runPnlByTracking() {
  loading.pnlByTracking = true;
  results.pnlByTracking = null;
  try {
    const result = await processStore.runProcess('pnl-by-tracking', {
      tenantId: dataStore.selectedTenant,
      from_date: pnlTrackingOptions.fromDate || undefined,
      to_date: pnlTrackingOptions.toDate || undefined,
    });
    results.pnlByTracking = result;
  } finally {
    loading.pnlByTracking = false;
  }
}

async function runReconcile() {
  loading.reconcile = true;
  results.reconcile = null;
  try {
    const result = await processStore.runProcess('reconcile', {
      tenantId: dataStore.selectedTenant,
      financial_year: reconcileOptions.financialYear,
      fiscal_year_start_month: reconcileOptions.fiscalYearStartMonth,
      tolerance: reconcileOptions.tolerance,
    });
    results.reconcile = result;
  } finally {
    loading.reconcile = false;
  }
}
</script>
