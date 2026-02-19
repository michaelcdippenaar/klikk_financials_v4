<template>
  <q-page class="q-pa-md">
    <div class="text-h5 q-mb-md">Investec - Transactions</div>

    <div class="text-subtitle1 q-mb-sm">Upload transactions</div>
    <div class="row q-col-gutter-md q-mb-lg">
      <q-card class="col-12 col-md-4">
        <q-card-section>
          <q-file
            v-model="uploadFile"
            label="Excel file"
            accept=".xlsx,.xls"
            outlined
            dense
            clearable
            class="q-mb-sm"
          />
          <q-btn
            label="Upload"
            color="primary"
            :loading="loadingUpload"
            :disable="!uploadFile"
            @click="uploadTransactions"
          />
          <div v-if="uploadResult" class="q-mt-sm text-body2">
            <q-banner
              :class="uploadResult.error ? 'bg-negative' : 'bg-positive'"
              rounded
              dense
              class="text-white"
            >
              {{ uploadResult.error || uploadResult.message }}
            </q-banner>
          </div>
        </q-card-section>
      </q-card>
    </div>

    <div class="text-subtitle1 q-mb-sm">Transactions</div>
    <q-card class="q-mb-lg">
      <q-card-section>
        <div class="row q-col-gutter-sm q-mb-md">
          <q-input
            v-model="filters.account_number"
            label="Account number"
            outlined
            dense
            clearable
            class="col-12 col-sm-4"
            @update:model-value="fetchTransactions"
          />
          <q-input
            v-model="filters.share_name"
            label="Share name"
            outlined
            dense
            clearable
            class="col-12 col-sm-4"
            @update:model-value="fetchTransactions"
          />
          <q-input
            v-model="filters.type"
            label="Type"
            outlined
            dense
            clearable
            class="col-12 col-sm-4"
            @update:model-value="fetchTransactions"
          />
        </div>
        <q-table
          :rows="transactions"
          :columns="transactionColumns"
          row-key="id"
          flat
          bordered
          :loading="loadingTable"
          :pagination="pagination"
          @request="onTableRequest"
        >
          <template v-slot:body-cell-date="props">
            <q-td :props="props">{{ formatDate(props.row.date) }}</q-td>
          </template>
          <template v-slot:body-cell-value="props">
            <q-td :props="props">{{ formatNumber(props.row.value) }}</q-td>
          </template>
          <template v-slot:body-cell-quantity="props">
            <q-td :props="props">{{ formatNumber(props.row.quantity) }}</q-td>
          </template>
        </q-table>
        <div class="row justify-between items-center q-mt-sm">
          <q-btn
            flat
            dense
            :disable="pagination.offset === 0"
            @click="goPage(-1)"
          >
            Previous
          </q-btn>
          <span class="text-body2">
            {{ pagination.offset + 1 }}–{{ Math.min(pagination.offset + pagination.rowsPerPage, transactionCount) }} of {{ transactionCount }}
          </span>
          <q-btn
            flat
            dense
            :disable="pagination.offset + pagination.rowsPerPage >= transactionCount"
            @click="goPage(1)"
          >
            Next
          </q-btn>
        </div>
      </q-card-section>
    </q-card>

    <div class="text-subtitle1 q-mb-sm">Exports</div>
    <q-card>
      <q-card-section>
        <div class="row q-col-gutter-md">
          <div class="col-12 col-md-4">
            <q-btn
              label="Export companies"
              color="primary"
              outline
              :loading="loadingExportCompanies"
              @click="exportCompanies"
            />
            <div v-if="exportData.companies" class="q-mt-sm text-body2">
              Count: {{ exportData.companies.count }}
              <q-list v-if="exportData.companies.companies?.length" dense class="q-mt-xs">
                <q-item v-for="(c, i) in exportData.companies.companies.slice(0, 10)" :key="i" dense>
                  <q-item-section>{{ c.company }} ({{ c.share_code }})</q-item-section>
                </q-item>
                <q-item v-if="exportData.companies.companies.length > 10" dense>
                  <q-item-section class="text-grey">... and {{ exportData.companies.companies.length - 10 }} more</q-item-section>
                </q-item>
              </q-list>
            </div>
          </div>
          <div class="col-12 col-md-4">
            <q-btn
              label="Export share names"
              color="primary"
              outline
              :loading="loadingExportShareNames"
              @click="exportShareNames"
            />
            <div v-if="exportData.shareNames" class="q-mt-sm text-body2">
              Count: {{ exportData.shareNames.count }}
              <q-list v-if="exportData.shareNames.share_names?.length" dense class="q-mt-xs">
                <q-item v-for="(s, i) in exportData.shareNames.share_names.slice(0, 10)" :key="i" dense>
                  <q-item-section>{{ s }}</q-item-section>
                </q-item>
                <q-item v-if="exportData.shareNames.share_names.length > 10" dense>
                  <q-item-section class="text-grey">... and {{ exportData.shareNames.share_names.length - 10 }} more</q-item-section>
                </q-item>
              </q-list>
            </div>
          </div>
          <div class="col-12 col-md-4">
            <q-btn
              label="Export transactions (Excel)"
              color="primary"
              outline
              :loading="loadingExportTransactions"
              @click="exportTransactions"
            />
            <div v-if="exportData.transactions" class="q-mt-sm text-body2">
              <q-banner
                v-if="exportData.transactions.error"
                class="bg-negative text-white"
                rounded
                dense
              >
                {{ exportData.transactions.error }}
              </q-banner>
              <template v-else>
                <span>{{ exportData.transactions.message }}</span>
                <q-btn
                  v-if="exportData.transactions.filename"
                  flat
                  dense
                  color="primary"
                  :href="exportDownloadUrl(exportData.transactions.filename)"
                  target="_blank"
                  download
                  class="q-ml-sm"
                >
                  Download
                </q-btn>
              </template>
            </div>
          </div>
        </div>
      </q-card-section>
    </q-card>
  </q-page>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue';
import {
  getInvestecTransactions,
  uploadInvestecTransactions,
  getInvestecExportCompanies,
  getInvestecExportShareNames,
  getInvestecExportTransactions,
  getInvestecExportDownloadUrl,
} from '../api/endpoints';

const transactionColumns = [
  { name: 'date', label: 'Date', field: 'date', align: 'left', sortable: true },
  { name: 'account_number', label: 'Account', field: 'account_number', align: 'left' },
  { name: 'description', label: 'Description', field: 'description', align: 'left' },
  { name: 'share_name', label: 'Share name', field: 'share_name', align: 'left' },
  { name: 'type', label: 'Type', field: 'type', align: 'left' },
  { name: 'quantity', label: 'Quantity', field: 'quantity', align: 'right' },
  { name: 'value', label: 'Value', field: 'value', align: 'right' },
];

const transactions = ref([]);
const transactionCount = ref(0);
const uploadFile = ref(null);
const uploadResult = ref(null);
const loadingUpload = ref(false);
const loadingTable = ref(false);
const loadingExportCompanies = ref(false);
const loadingExportShareNames = ref(false);
const loadingExportTransactions = ref(false);

const filters = reactive({
  account_number: '',
  share_name: '',
  type: '',
});

const pagination = reactive({
  offset: 0,
  rowsPerPage: 100,
});

const exportData = reactive({
  companies: null,
  shareNames: null,
  transactions: null,
});

function formatDate(val) {
  if (!val) return '';
  return new Date(val).toLocaleDateString();
}

function formatNumber(val) {
  if (val == null) return '';
  const n = Number(val);
  return isNaN(n) ? val : n.toLocaleString();
}

function exportDownloadUrl(filename) {
  return getInvestecExportDownloadUrl(filename);
}

async function fetchTransactions() {
  loadingTable.value = true;
  try {
    const data = await getInvestecTransactions({
      limit: pagination.rowsPerPage,
      offset: pagination.offset,
      account_number: filters.account_number || undefined,
      share_name: filters.share_name || undefined,
      type: filters.type || undefined,
    });
    transactions.value = data.results || [];
    transactionCount.value = data.count ?? 0;
  } catch (err) {
    transactions.value = [];
    transactionCount.value = 0;
    console.error(err);
  } finally {
    loadingTable.value = false;
  }
}

function onTableRequest(props) {
  pagination.offset = props.pagination.offset ?? 0;
  pagination.rowsPerPage = props.pagination.rowsPerPage ?? 100;
  fetchTransactions();
}

function goPage(delta) {
  pagination.offset = Math.max(0, pagination.offset + delta * pagination.rowsPerPage);
  fetchTransactions();
}

async function uploadTransactions() {
  if (!uploadFile.value) return;
  loadingUpload.value = true;
  uploadResult.value = null;
  try {
    const data = await uploadInvestecTransactions(uploadFile.value);
    uploadResult.value = {
      message: data.message || (data.created != null ? `Created ${data.created} transaction(s).` : 'Upload successful.'),
    };
    uploadFile.value = null;
    fetchTransactions();
  } catch (err) {
    uploadResult.value = {
      error: err.response?.data?.error || err.message || 'Upload failed.',
    };
  } finally {
    loadingUpload.value = false;
  }
}

async function exportCompanies() {
  loadingExportCompanies.value = true;
  exportData.companies = null;
  try {
    const data = await getInvestecExportCompanies();
    exportData.companies = data;
  } catch (err) {
    exportData.companies = { count: 0, companies: [], error: err.response?.data?.error || err.message };
  } finally {
    loadingExportCompanies.value = false;
  }
}

async function exportShareNames() {
  loadingExportShareNames.value = true;
  exportData.shareNames = null;
  try {
    const data = await getInvestecExportShareNames();
    exportData.shareNames = data;
  } catch (err) {
    exportData.shareNames = { count: 0, share_names: [], error: err.response?.data?.error || err.message };
  } finally {
    loadingExportShareNames.value = false;
  }
}

async function exportTransactions() {
  loadingExportTransactions.value = true;
  exportData.transactions = null;
  try {
    const data = await getInvestecExportTransactions();
    exportData.transactions = {
      message: data.message || `Exported ${data.count ?? 0} transactions.`,
      filename: data.filename,
    };
  } catch (err) {
    exportData.transactions = {
      error: err.response?.data?.error || err.message || 'Export failed.',
    };
  } finally {
    loadingExportTransactions.value = false;
  }
}

onMounted(() => {
  fetchTransactions();
});
</script>
