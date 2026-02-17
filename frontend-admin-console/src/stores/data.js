import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { getSummary, getTrailBalance, getLineItems, getTenants, getPnlSummary, getAccountSummary } from '../api/endpoints';
import { STORAGE_KEYS } from '../utils/constants';

export const useDataStore = defineStore('data', () => {
  const selectedTenant = ref(localStorage.getItem(STORAGE_KEYS.SELECTED_TENANT) || null);
  const tenants = ref([]);
  const summary = ref(null);
  const trailBalance = ref(null);
  const lineItems = ref(null);
  const pnlSummary = ref(null);
  const accountSummary = ref(null);
  const loading = ref(false);
  const error = ref(null);

  // Load tenants on init
  async function loadTenants() {
    try {
      const data = await getTenants();
      tenants.value = data;
      return { success: true };
    } catch (err) {
      error.value = err.response?.data?.error || err.message;
      return { success: false, error: error.value };
    }
  }

  function setSelectedTenant(tenantId) {
    selectedTenant.value = tenantId;
    if (tenantId) {
      localStorage.setItem(STORAGE_KEYS.SELECTED_TENANT, tenantId);
    } else {
      localStorage.removeItem(STORAGE_KEYS.SELECTED_TENANT);
    }
    // Clear data when tenant changes
    summary.value = null;
    trailBalance.value = null;
    lineItems.value = null;
    pnlSummary.value = null;
    accountSummary.value = null;
  }

  const selectedTenantName = computed(() => {
    if (!selectedTenant.value) return null;
    const tenant = tenants.value.find(t => t.tenant_id === selectedTenant.value);
    return tenant?.tenant_name || null;
  });

  async function fetchSummary() {
    if (!selectedTenant.value) {
      error.value = 'No tenant selected';
      return { success: false, error: error.value };
    }

    loading.value = true;
    error.value = null;
    try {
      const data = await getSummary(selectedTenant.value);
      summary.value = data;
      return { success: true, data };
    } catch (err) {
      error.value = err.response?.data?.error || err.message;
      return { success: false, error: error.value };
    } finally {
      loading.value = false;
    }
  }

  async function fetchTrailBalance(filters = {}) {
    if (!selectedTenant.value) {
      error.value = 'No tenant selected';
      return { success: false, error: error.value };
    }

    loading.value = true;
    error.value = null;
    try {
      const data = await getTrailBalance(selectedTenant.value, filters);
      trailBalance.value = data;
      return { success: true, data };
    } catch (err) {
      error.value = err.response?.data?.error || err.message;
      return { success: false, error: error.value };
    } finally {
      loading.value = false;
    }
  }

  async function fetchAccountSummary(filters = {}) {
    if (!selectedTenant.value) {
      error.value = 'No tenant selected';
      return { success: false, error: error.value };
    }

    loading.value = true;
    error.value = null;
    try {
      const data = await getAccountSummary(selectedTenant.value, filters);
      accountSummary.value = data;
      return { success: true, data };
    } catch (err) {
      error.value = err.response?.data?.error || err.message;
      return { success: false, error: error.value };
    } finally {
      loading.value = false;
    }
  }

  async function fetchPnlSummary(filters = {}) {
    if (!selectedTenant.value) {
      error.value = 'No tenant selected';
      return { success: false, error: error.value };
    }

    loading.value = true;
    error.value = null;
    try {
      const data = await getPnlSummary(selectedTenant.value, filters);
      pnlSummary.value = data;
      return { success: true, data };
    } catch (err) {
      error.value = err.response?.data?.error || err.message;
      return { success: false, error: error.value };
    } finally {
      loading.value = false;
    }
  }

  async function fetchLineItems(filters = {}) {
    if (!selectedTenant.value) {
      error.value = 'No tenant selected';
      return { success: false, error: error.value };
    }

    loading.value = true;
    error.value = null;
    try {
      const data = await getLineItems(selectedTenant.value, filters);
      lineItems.value = data;
      return { success: true, data };
    } catch (err) {
      error.value = err.response?.data?.error || err.message;
      return { success: false, error: error.value };
    } finally {
      loading.value = false;
    }
  }

  return {
    selectedTenant,
    selectedTenantName,
    tenants,
    summary,
    trailBalance,
    lineItems,
    pnlSummary,
    accountSummary,
    loading,
    error,
    loadTenants,
    setSelectedTenant,
    fetchSummary,
    fetchAccountSummary,
    fetchTrailBalance,
    fetchPnlSummary,
    fetchLineItems,
  };
});
