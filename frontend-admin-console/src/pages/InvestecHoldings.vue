<template>
  <q-page class="q-pa-md">
    <div class="text-h5 q-mb-md">Investec - Holdings</div>

    <div class="text-subtitle1 q-mb-sm">Upload portfolio</div>
    <q-card class="q-mb-lg" style="max-width: 400px">
      <q-card-section>
        <q-file
          v-model="uploadFile"
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
          :loading="loading"
          :disable="!(Array.isArray(uploadFile) ? uploadFile.length : uploadFile)"
          @click="uploadPortfolio"
        />
        <div v-if="result" class="q-mt-sm">
          <q-banner
            :class="result.error ? 'bg-negative' : 'bg-positive'"
            rounded
            dense
            class="text-white"
          >
            {{ result.error || result.message }}
          </q-banner>
        </div>
      </q-card-section>
    </q-card>
  </q-page>
</template>

<script setup>
import { ref } from 'vue';
import { uploadInvestecPortfolio } from '../api/endpoints';

const uploadFile = ref(null);
const loading = ref(false);
const result = ref(null);

async function uploadPortfolio() {
  const files = uploadFile.value;
  if (!files?.length) return;
  const fileList = Array.isArray(files) ? files : [files];
  loading.value = true;
  result.value = null;
  try {
    const data = await uploadInvestecPortfolio(fileList);
    const msg = data.success
      ? `Processed ${data.successful_files ?? fileList.length} file(s).`
      : (data.message || (data.failed_files ? `Some failed: ${data.failed_files}.` : 'Upload completed.'));
    result.value = { message: msg };
    uploadFile.value = null;
  } catch (err) {
    result.value = {
      error: err.response?.data?.error || err.message || 'Upload failed.',
    };
  } finally {
    loading.value = false;
  }
}
</script>
