<template>
  <q-page class="q-pa-md">
    <div class="text-h5 q-mb-md">Investec</div>

    <q-tabs
      v-model="activeTab"
      dense
      class="text-grey"
      active-color="primary"
      indicator-color="primary"
      align="left"
      narrow-indicator
      @update:model-value="onTabChange"
    >
      <q-tab name="data" label="Data" />
      <q-tab name="mappings" label="Share codes & names" />
    </q-tabs>

    <q-separator class="q-mb-md" />

    <template v-if="activeTab === 'data'">
    <!-- Uploads -->
    <div class="text-subtitle1 q-mb-sm">Uploads</div>
    <div class="row q-col-gutter-md q-mb-lg">
      <q-card class="col-12 col-md-4">
        <q-card-section>
          <div class="text-subtitle2 q-mb-sm">Transactions</div>
          <q-file
            v-model="uploadFiles.transactions"
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
            :loading="loading.uploadTransactions"
            :disable="!uploadFiles.transactions"
            @click="uploadTransactions"
          />
          <div v-if="uploadResult.transactions" class="q-mt-sm text-body2">
            <q-banner
              :class="uploadResult.transactions.error ? 'bg-negative' : 'bg-positive'"
              rounded
              dense
              class="text-white"
            >
              {{ uploadResult.transactions.error || uploadResult.transactions.message }}
            </q-banner>
          </div>
        </q-card-section>
      </q-card>
      <q-card class="col-12 col-md-4">
        <q-card-section>
          <div class="text-subtitle2 q-mb-sm">Portfolio</div>
          <q-file
            v-model="uploadFiles.portfolio"
            label="Excel file(s)"
            accept=".xlsx,.xls"
            outlined
            dense
            clearable
            multiple
            class="q-mb-sm"
          />
          <q-btn
            label="Upload"
            color="primary"
            :loading="loading.uploadPortfolio"
            :disable="!(Array.isArray(uploadFiles.portfolio) ? uploadFiles.portfolio.length : uploadFiles.portfolio)"
            @click="uploadPortfolio"
          />
          <div v-if="uploadResult.portfolio" class="q-mt-sm text-body2">
            <q-banner
              :class="uploadResult.portfolio.error ? 'bg-negative' : 'bg-positive'"
              rounded
              dense
              class="text-white"
            >
              {{ uploadResult.portfolio.error || uploadResult.portfolio.message }}
            </q-banner>
          </div>
        </q-card-section>
      </q-card>
      <q-card class="col-12 col-md-4">
        <q-card-section>
          <div class="text-subtitle2 q-mb-sm">Share name mapping</div>
          <q-file
            v-model="uploadFiles.mapping"
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
            :loading="loading.uploadMapping"
            :disable="!uploadFiles.mapping"
            @click="uploadMapping"
          />
          <div v-if="uploadResult.mapping" class="q-mt-sm text-body2">
            <q-banner
              :class="uploadResult.mapping.error ? 'bg-negative' : 'bg-positive'"
              rounded
              dense
              class="text-white"
            >
              {{ uploadResult.mapping.error || uploadResult.mapping.message }}
            </q-banner>
          </div>
        </q-card-section>
      </q-card>
    </div>

    <!-- Transactions table -->
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
          :loading="loading.transactions"
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

    <!-- Exports -->
    <div class="text-subtitle1 q-mb-sm">Exports</div>
    <q-card>
      <q-card-section>
        <div class="row q-col-gutter-md">
          <div class="col-12 col-md-4">
            <q-btn
              label="Export companies"
              color="primary"
              outline
              :loading="loading.exportCompanies"
              @click="exportCompanies"
            />
            <div v-if="exportData.companies" class="q-mt-sm text-body2">
              Count: {{ exportData.companies.count }}
              <q-list v-if="exportData.companies.companies?.length" dense class="q-mt-xs">
                <q-item v-for="(c, i) in exportData.companies.companies.slice(0, 10)" :key="i" dense>
                  <q-item-section>{{ c.company }} ({{ c.share_code }})</q-item-section>
                </q-item>
                <q-item v-if="exportData.companies.companies.length > 10" dense>
                  <q-item-section class="text-grey">… and {{ exportData.companies.companies.length - 10 }} more</q-item-section>
                </q-item>
              </q-list>
            </div>
          </div>
          <div class="col-12 col-md-4">
            <q-btn
              label="Export share names"
              color="primary"
              outline
              :loading="loading.exportShareNames"
              @click="exportShareNames"
            />
            <div v-if="exportData.shareNames" class="q-mt-sm text-body2">
              Count: {{ exportData.shareNames.count }}
              <q-list v-if="exportData.shareNames.share_names?.length" dense class="q-mt-xs">
                <q-item v-for="(s, i) in exportData.shareNames.share_names.slice(0, 10)" :key="i" dense>
                  <q-item-section>{{ s }}</q-item-section>
                </q-item>
                <q-item v-if="exportData.shareNames.share_names.length > 10" dense>
                  <q-item-section class="text-grey">… and {{ exportData.shareNames.share_names.length - 10 }} more</q-item-section>
                </q-item>
              </q-list>
            </div>
          </div>
          <div class="col-12 col-md-4">
            <q-btn
              label="Export transactions (Excel)"
              color="primary"
              outline
              :loading="loading.exportTransactions"
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
    </template>

    <template v-if="activeTab === 'mappings'">
      <q-card class="q-mb-md" v-if="unmappedShareNames.length > 0">
        <q-card-section>
          <div class="row items-center justify-between q-mb-sm">
            <div class="text-subtitle2">Share names in transactions not in mapping ({{ unmappedShareNames.length }})</div>
            <q-btn
              label="Refresh"
              color="primary"
              flat
              dense
              size="sm"
              :loading="loading.unmappedShareNames"
              @click="fetchUnmappedShareNames"
            />
          </div>
          <div class="text-body2 text-grey-8">
            Add these to the mapping table (e.g. via upload) so they have a share code and company.
          </div>
          <q-list dense class="q-mt-sm">
            <q-item v-for="(name, i) in unmappedShareNames" :key="i" dense>
              <q-item-section>{{ name }}</q-item-section>
            </q-item>
          </q-list>
        </q-card-section>
      </q-card>
      <q-card>
        <q-card-section>
          <div class="row items-center justify-between q-mb-md">
            <div class="text-subtitle1">Share codes, names and company</div>
            <q-btn
              label="Refresh"
              color="primary"
              flat
              dense
              :loading="loading.mappings"
              @click="refreshMappingsTab"
            />
          </div>
          <q-table
            :rows="mappings"
            :columns="mappingColumns"
            row-key="id"
            flat
            bordered
            :loading="loading.mappings"
            :rows-per-page-options="[25, 50, 100]"
          />
        </q-card-section>
      </q-card>
    </template>
  </q-page>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue';
import {
  getInvestecTransactions,
  getInvestecMappings,
  getInvestecUnmappedShareNames,
  uploadInvestecTransactions as apiUploadTransactions,
  uploadInvestecPortfolio as apiUploadPortfolio,
  uploadInvestecMapping as apiUploadMapping,
  getInvestecExportCompanies,
  getInvestecExportShareNames,
  getInvestecExportTransactions,
  getInvestecExportDownloadUrl,
} from '../api/endpoints';

const activeTab = ref('data');

const mappingColumns = [
  { name: 'share_name', label: 'Share name', field: 'share_name', align: 'left', sortable: true },
  { name: 'company', label: 'Company', field: 'company', align: 'left', sortable: true },
  { name: 'share_code', label: 'Share code', field: 'share_code', align: 'left', sortable: true },
];

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
const mappings = ref([]);
const unmappedShareNames = ref([]);
const loading = reactive({
  transactions: false,
  mappings: false,
  unmappedShareNames: false,
  uploadTransactions: false,
  uploadPortfolio: false,
  uploadMapping: false,
  exportCompanies: false,
  exportShareNames: false,
  exportTransactions: false,
});

const uploadFiles = reactive({
  transactions: null,
  portfolio: null,
  mapping: null,
});

const uploadResult = reactive({
  transactions: null,
  portfolio: null,
  mapping: null,
});

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
  loading.transactions = true;
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
    loading.transactions = false;
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
  const file = uploadFiles.transactions;
  if (!file) return;
  loading.uploadTransactions = true;
  uploadResult.transactions = null;
  try {
    const data = await apiUploadTransactions(file);
    uploadResult.transactions = {
      message: data.message || (data.created != null ? `Created ${data.created} transaction(s).` : 'Upload successful.'),
    };
    uploadFiles.transactions = null;
    fetchTransactions();
  } catch (err) {
    uploadResult.transactions = {
      error: err.response?.data?.error || err.message || 'Upload failed.',
    };
  } finally {
    loading.uploadTransactions = false;
  }
}

async function uploadPortfolio() {
  const files = uploadFiles.portfolio;
  if (!files?.length) return;
  const fileList = Array.isArray(files) ? files : [files];
  loading.uploadPortfolio = true;
  uploadResult.portfolio = null;
  try {
    const data = await apiUploadPortfolio(fileList);
    const msg = data.success
      ? `Processed ${data.successful_files ?? fileList.length} file(s).`
      : (data.message || (data.failed_files ? `Some failed: ${data.failed_files}.` : 'Upload completed.'));
    uploadResult.portfolio = { message: msg };
    uploadFiles.portfolio = null;
  } catch (err) {
    uploadResult.portfolio = {
      error: err.response?.data?.error || err.message || 'Upload failed.',
    };
  } finally {
    loading.uploadPortfolio = false;
  }
}

async function uploadMapping() {
  const file = uploadFiles.mapping;
  if (!file) return;
  loading.uploadMapping = true;
  uploadResult.mapping = null;
  try {
    const data = await apiUploadMapping(file);
    const created = data.created ?? 0;
    const updated = data.updated ?? 0;
    uploadResult.mapping = {
      message: data.message || `Created ${created}, updated ${updated} mapping(s).`,
    };
    uploadFiles.mapping = null;
  } catch (err) {
    uploadResult.mapping = {
      error: err.response?.data?.error || err.message || 'Upload failed.',
    };
  } finally {
    loading.uploadMapping = false;
  }
  refreshMappingsTab();
}

async function fetchMappings() {
  loading.mappings = true;
  try {
    const data = await getInvestecMappings();
    mappings.value = data.results || [];
  } catch (err) {
    mappings.value = [];
    console.error(err);
  } finally {
    loading.mappings = false;
  }
}

async function fetchUnmappedShareNames() {
  loading.unmappedShareNames = true;
  try {
    const data = await getInvestecUnmappedShareNames();
    unmappedShareNames.value = data.share_names || [];
  } catch (err) {
    unmappedShareNames.value = [];
    console.error(err);
  } finally {
    loading.unmappedShareNames = false;
  }
}

function refreshMappingsTab() {
  fetchMappings();
  fetchUnmappedShareNames();
}

function onTabChange(tab) {
  if (tab === 'mappings') refreshMappingsTab();
}

async function exportCompanies() {
  loading.exportCompanies = true;
  exportData.companies = null;
  try {
    const data = await getInvestecExportCompanies();
    exportData.companies = data;
  } catch (err) {
    exportData.companies = { count: 0, companies: [], error: err.response?.data?.error || err.message };
  } finally {
    loading.exportCompanies = false;
  }
}

async function exportShareNames() {
  loading.exportShareNames = true;
  exportData.shareNames = null;
  try {
    const data = await getInvestecExportShareNames();
    exportData.shareNames = data;
  } catch (err) {
    exportData.shareNames = { count: 0, share_names: [], error: err.response?.data?.error || err.message };
  } finally {
    loading.exportShareNames = false;
  }
}

async function exportTransactions() {
  loading.exportTransactions = true;
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
    loading.exportTransactions = false;
  }
}

onMounted(() => {
  fetchTransactions();
});
</script>
