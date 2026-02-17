<template>
  <q-page class="q-pa-md">
    <div class="text-h4 q-mb-md">Xero Connection</div>

    <!-- Callback result banner -->
    <q-banner
      v-if="callbackStatus === 'success'"
      class="bg-positive text-white q-mb-md"
      rounded
      dense
    >
      <template v-slot:avatar>
        <q-icon name="check_circle" />
      </template>
      <strong>Connected successfully!</strong>
      {{ callbackTenants ? `Tenants: ${callbackTenants}` : '' }}
      <template v-slot:action>
        <q-btn flat label="Dismiss" @click="clearCallback" />
      </template>
    </q-banner>

    <q-banner
      v-if="callbackStatus === 'error'"
      class="bg-negative text-white q-mb-md"
      rounded
      dense
    >
      <template v-slot:avatar>
        <q-icon name="error" />
      </template>
      <strong>Connection failed:</strong> {{ callbackMessage }}
      <template v-slot:action>
        <q-btn flat label="Dismiss" @click="clearCallback" />
      </template>
    </q-banner>

    <!-- Step 1: API Credentials Setup -->
    <q-card class="q-mb-md">
      <q-card-section>
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-avatar
            :color="connectionStatus?.has_credentials ? 'positive' : 'grey-4'"
            text-color="white"
            size="sm"
            font-size="14px"
          >1</q-avatar>
          <div class="text-h6">API Credentials</div>
          <q-badge
            v-if="connectionStatus?.has_credentials"
            color="positive"
            label="Configured"
          />
        </div>
        <div class="text-body2 text-grey-7 q-mb-md">
          Enter your Xero app's Client ID and Client Secret.
          Get these from the
          <a href="https://developer.xero.com/app/manage" target="_blank" class="text-primary">
            Xero Developer Portal
          </a>.
        </div>

        <q-form @submit.prevent="saveCredentials" class="row q-gutter-sm items-end">
          <q-input
            v-model="clientId"
            label="Client ID"
            outlined
            dense
            class="col-12 col-sm"
            :hint="connectionStatus?.credentials_client_id
              ? `Current: ${connectionStatus.credentials_client_id}`
              : ''"
          />
          <q-input
            v-model="clientSecret"
            label="Client Secret"
            outlined
            dense
            type="password"
            class="col-12 col-sm"
          />
          <q-btn
            label="Save"
            color="primary"
            type="submit"
            :loading="savingCredentials"
            :disable="!clientId || !clientSecret"
            icon="save"
            class="q-mb-xs"
          />
        </q-form>
      </q-card-section>
    </q-card>

    <!-- Step 2: Connect to Xero -->
    <q-card class="q-mb-md">
      <q-card-section>
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-avatar
            :color="connectionStatus?.connected ? 'positive' : 'grey-4'"
            text-color="white"
            size="sm"
            font-size="14px"
          >2</q-avatar>
          <div class="text-h6">Connect to Xero</div>
          <q-badge
            v-if="connectionStatus?.connected"
            color="positive"
            label="Connected"
          />
        </div>
        <div class="text-body2 text-grey-7 q-mb-md">
          Authorize access to your Xero organisation. This will redirect you to Xero to log in.
        </div>

        <div class="row items-center q-gutter-sm">
          <q-btn
            :label="connectionStatus?.connected ? 'Reconnect to Xero' : 'Connect to Xero'"
            color="primary"
            icon="link"
            :loading="connecting"
            :disable="!connectionStatus?.has_credentials"
            @click="connectToXero"
          />
          <q-btn
            flat
            icon="refresh"
            color="grey-7"
            :loading="loadingStatus"
            @click="loadConnectionStatus"
          >
            <q-tooltip>Refresh status</q-tooltip>
          </q-btn>
          <span
            v-if="!connectionStatus?.has_credentials"
            class="text-caption text-grey-6"
          >
            Save your API credentials first
          </span>
        </div>
      </q-card-section>
    </q-card>

    <!-- Connected Tenants -->
    <q-card v-if="connectionStatus?.tenants?.length > 0">
      <q-card-section>
        <div class="text-h6 q-mb-sm">Connected Tenants</div>
      </q-card-section>

      <q-list separator>
        <q-item v-for="tenant in connectionStatus.tenants" :key="tenant.tenant_id">
          <q-item-section avatar>
            <q-icon
              :name="tenant.token_expired ? 'warning' : 'check_circle'"
              :color="tenant.token_expired ? 'warning' : 'positive'"
            />
          </q-item-section>
          <q-item-section>
            <q-item-label>{{ tenant.tenant_name }}</q-item-label>
            <q-item-label caption>
              ID: {{ tenant.tenant_id.substring(0, 8) }}...
            </q-item-label>
          </q-item-section>
          <q-item-section side>
            <q-badge
              :color="tenant.token_expired ? 'warning' : 'positive'"
              :label="tenant.token_expired ? 'Token expired' : 'Active'"
            />
          </q-item-section>
          <q-item-section side v-if="tenant.connected_at">
            <q-item-label caption>
              {{ formatDate(tenant.connected_at) }}
            </q-item-label>
          </q-item-section>
        </q-item>
      </q-list>
    </q-card>

    <!-- Loading skeleton -->
    <q-card v-if="loadingStatus && !connectionStatus">
      <q-card-section>
        <q-skeleton type="text" width="200px" />
        <q-skeleton type="text" width="300px" class="q-mt-sm" />
      </q-card-section>
    </q-card>
  </q-page>
</template>

<script setup>
import { ref, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useQuasar } from 'quasar';
import { useDataStore } from '../stores/data';
import { getXeroAuthUrl, getXeroConnectionStatus, saveXeroCredentials } from '../api/endpoints';

const route = useRoute();
const router = useRouter();
const $q = useQuasar();
const dataStore = useDataStore();

const connectionStatus = ref(null);
const loadingStatus = ref(false);
const connecting = ref(false);
const savingCredentials = ref(false);

// Credentials form
const clientId = ref('');
const clientSecret = ref('');

// Callback params from URL query
const callbackStatus = ref(route.query.status || null);
const callbackTenants = ref(route.query.tenants || null);
const callbackMessage = ref(route.query.message || null);

function clearCallback() {
  callbackStatus.value = null;
  callbackTenants.value = null;
  callbackMessage.value = null;
  router.replace({ query: {} });
}

async function loadConnectionStatus() {
  loadingStatus.value = true;
  try {
    connectionStatus.value = await getXeroConnectionStatus();
  } catch (err) {
    console.error('Failed to load connection status:', err);
  } finally {
    loadingStatus.value = false;
  }
}

async function saveCredentials() {
  if (!clientId.value || !clientSecret.value) return;

  savingCredentials.value = true;
  try {
    const result = await saveXeroCredentials(clientId.value, clientSecret.value);
    $q.notify({
      type: 'positive',
      message: result.message || 'Credentials saved',
      icon: 'check_circle',
    });
    // Clear form and reload status
    clientId.value = '';
    clientSecret.value = '';
    await loadConnectionStatus();
  } catch (err) {
    $q.notify({
      type: 'negative',
      message: err.response?.data?.error || 'Failed to save credentials',
      icon: 'error',
    });
  } finally {
    savingCredentials.value = false;
  }
}

async function connectToXero() {
  connecting.value = true;
  try {
    const data = await getXeroAuthUrl();
    if (data.auth_url) {
      window.location.href = data.auth_url;
    }
  } catch (err) {
    console.error('Failed to initiate Xero auth:', err);
    $q.notify({
      type: 'negative',
      message: err.response?.data?.error || 'Failed to start Xero connection',
      icon: 'error',
    });
  } finally {
    connecting.value = false;
  }
}

function formatDate(dateStr) {
  if (!dateStr) return '';
  try {
    return new Date(dateStr).toLocaleDateString('en-ZA', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return dateStr;
  }
}

onMounted(async () => {
  await loadConnectionStatus();
  if (callbackStatus.value === 'success') {
    await dataStore.loadTenants();
  }
});
</script>
