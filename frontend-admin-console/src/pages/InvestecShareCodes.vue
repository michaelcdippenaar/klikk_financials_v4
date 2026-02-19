<template>
  <q-page class="q-pa-md">
    <div class="text-h5 q-mb-md">Investec - Share codes</div>

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
      <q-tab name="share-codes" label="Share codes" />
      <q-tab name="unmapped" label="Share names in transactions not in mapping" />
    </q-tabs>

    <q-separator class="q-mb-md" />

    <template v-if="activeTab === 'share-codes'">
      <q-card>
        <q-card-section>
          <div class="row items-center justify-between q-mb-md flex-wrap q-col-gutter-sm">
            <div class="text-subtitle1">Share codes, names and company</div>
            <div class="row no-wrap q-gutter-sm">
              <q-btn
                label="Download to Excel"
                color="primary"
                outline
                :loading="loadingExport"
                @click="downloadMapping"
              />
              <q-btn
                label="Upload"
                color="primary"
                :loading="loadingUpload"
                :disable="!uploadFile"
                @click="uploadMapping"
              />
              <q-file
                v-model="uploadFile"
                label="Excel file"
                accept=".xlsx,.xls"
                outlined
                dense
                clearable
                class="col-auto"
                style="min-width: 160px"
              />
              <q-btn
                label="Refresh"
                color="primary"
                flat
                dense
                :loading="loadingMappings"
                @click="refreshShareCodes"
              />
            </div>
          </div>
          <p class="text-body2 text-grey-8 q-mb-md">
            Download the current mapping to Excel, edit (add/change Share_Name, Share_Name2, Share_Name3, Company, Share_Code), then upload to update.
            Share_Name2 and Share_Name3 are alternative names used for the same share in transaction files.
          </p>
          <div v-if="uploadResult" class="q-mb-sm">
            <q-banner
              :class="uploadResult.error ? 'bg-negative' : 'bg-positive'"
              rounded
              dense
              class="text-white"
            >
              {{ uploadResult.error || uploadResult.message }}
            </q-banner>
          </div>
          <div v-if="exportResult && exportResult.error" class="q-mb-sm">
            <q-banner class="bg-negative text-white" rounded dense>
              {{ exportResult.error }}
            </q-banner>
          </div>
          <q-table
            :rows="mappings"
            :columns="mappingColumns"
            row-key="id"
            flat
            bordered
            :loading="loadingMappings"
            :rows-per-page-options="[25, 50, 100]"
          />
        </q-card-section>
      </q-card>
    </template>

    <template v-if="activeTab === 'unmapped'">
      <q-card>
        <q-card-section>
          <div class="row items-center justify-between q-mb-sm">
            <div class="text-subtitle2">Share names in transactions not in mapping ({{ unmappedShareNames.length }})</div>
            <q-btn
              label="Refresh"
              color="primary"
              flat
              dense
              size="sm"
              :loading="loadingUnmapped"
              @click="fetchUnmappedShareNames"
            />
          </div>
          <div class="text-body2 text-grey-8">
            Add these to the mapping table: download Share codes above, add rows for these names (and Company/Share_Code if you have them), then upload.
          </div>
          <q-list dense class="q-mt-sm">
            <q-item v-for="(name, i) in unmappedShareNames" :key="i" dense>
              <q-item-section>{{ name }}</q-item-section>
            </q-item>
          </q-list>
        </q-card-section>
      </q-card>
    </template>
  </q-page>
</template>

<script setup>
import { ref, onMounted } from 'vue';
import {
  getInvestecMappings,
  getInvestecUnmappedShareNames,
  uploadInvestecMapping,
} from '../api/endpoints';
import { API_BASE_URL, API_ENDPOINTS } from '../utils/constants';

const activeTab = ref('share-codes');

const mappingColumns = [
  { name: 'share_name', label: 'Share name', field: 'share_name', align: 'left', sortable: true },
  { name: 'share_name2', label: 'Alt name 2', field: 'share_name2', align: 'left', sortable: true },
  { name: 'share_name3', label: 'Alt name 3', field: 'share_name3', align: 'left', sortable: true },
  { name: 'company', label: 'Company', field: 'company', align: 'left', sortable: true },
  { name: 'share_code', label: 'Share code', field: 'share_code', align: 'left', sortable: true },
];

const mappings = ref([]);
const unmappedShareNames = ref([]);
const uploadFile = ref(null);
const uploadResult = ref(null);
const exportResult = ref(null);
const loadingMappings = ref(false);
const loadingUnmapped = ref(false);
const loadingUpload = ref(false);
const loadingExport = ref(false);

async function fetchMappings() {
  loadingMappings.value = true;
  try {
    const data = await getInvestecMappings();
    mappings.value = data.results || [];
  } catch (err) {
    mappings.value = [];
    console.error(err);
  } finally {
    loadingMappings.value = false;
  }
}

async function fetchUnmappedShareNames() {
  loadingUnmapped.value = true;
  try {
    const data = await getInvestecUnmappedShareNames();
    unmappedShareNames.value = data.share_names || [];
  } catch (err) {
    unmappedShareNames.value = [];
    console.error(err);
  } finally {
    loadingUnmapped.value = false;
  }
}

function refreshShareCodes() {
  fetchMappings();
  fetchUnmappedShareNames();
}

function onTabChange(tab) {
  if (tab === 'unmapped') fetchUnmappedShareNames();
}

function downloadMapping() {
  const token = localStorage.getItem('auth_token');
  const url = `${API_BASE_URL}${API_ENDPOINTS.INVESTEC_EXPORT_MAPPING}`;
  // Use fetch to get the file with auth header, then trigger browser download
  loadingExport.value = true;
  exportResult.value = null;
  fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    .then((res) => {
      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="?([^"]+)"?/);
      const filename = match ? match[1] : 'Share_Codes_Names_Company.xlsx';
      return res.blob().then((blob) => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    })
    .catch((err) => {
      exportResult.value = { error: err.message || 'Export failed.' };
    })
    .finally(() => {
      loadingExport.value = false;
    });
}

async function uploadMapping() {
  const file = uploadFile.value;
  if (!file) return;
  loadingUpload.value = true;
  uploadResult.value = null;
  try {
    const data = await uploadInvestecMapping(file);
    uploadResult.value = {
      message: data.message || `Created ${data.created ?? 0}, updated ${data.updated ?? 0} mapping(s).`,
    };
    uploadFile.value = null;
    refreshShareCodes();
  } catch (err) {
    uploadResult.value = {
      error: err.response?.data?.error || err.message || 'Upload failed.',
    };
  } finally {
    loadingUpload.value = false;
  }
}

onMounted(() => {
  fetchMappings();
});
</script>
