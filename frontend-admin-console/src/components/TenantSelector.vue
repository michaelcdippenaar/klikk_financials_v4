<template>
  <q-select
    v-model="selectedTenantId"
    :options="tenantOptions"
    option-label="label"
    option-value="value"
    emit-value
    map-options
    label="Select Tenant"
    dense
    outlined
    class="q-mr-md"
    style="min-width: 250px"
    @update:model-value="onTenantChange"
  >
    <template v-slot:prepend>
      <q-icon name="business" />
    </template>
  </q-select>
</template>

<script setup>
import { computed, onMounted } from 'vue';
import { useDataStore } from '../stores/data';

const dataStore = useDataStore();

const selectedTenantId = computed({
  get: () => dataStore.selectedTenant,
  set: (value) => dataStore.setSelectedTenant(value),
});

const tenantOptions = computed(() => {
  return dataStore.tenants.map(tenant => ({
    label: tenant.tenant_name,
    value: tenant.tenant_id,
  }));
});

function onTenantChange(tenantId) {
  dataStore.setSelectedTenant(tenantId);
}

onMounted(async () => {
  if (dataStore.tenants.length === 0) {
    await dataStore.loadTenants();
  }
});
</script>
