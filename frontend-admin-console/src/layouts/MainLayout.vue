<template>
  <q-layout view="hHh lpR fFf">
    <q-header elevated class="bg-primary text-white">
      <q-toolbar>
        <q-toolbar-title class="text-weight-bold">
          Klikk Admin Console
        </q-toolbar-title>

        <q-space />

        <TenantSelector />

        <q-btn
          flat
          round
          dense
          icon="logout"
          @click="handleLogout"
        >
          <q-tooltip>Logout</q-tooltip>
        </q-btn>
      </q-toolbar>
    </q-header>

    <q-drawer
      v-model="leftDrawerOpen"
      show-if-above
      bordered
      class="bg-grey-1"
    >
      <q-list>
        <q-item-label header class="text-grey-8">
          Navigation
        </q-item-label>

        <q-item
          clickable
          v-ripple
          :to="{ name: 'dashboard' }"
          exact
        >
          <q-item-section avatar>
            <q-icon name="dashboard" />
          </q-item-section>
          <q-item-section>
            <q-item-label>Dashboard</q-item-label>
          </q-item-section>
        </q-item>

        <q-item
          clickable
          v-ripple
          :to="{ name: 'processes' }"
        >
          <q-item-section avatar>
            <q-icon name="play_circle" />
          </q-item-section>
          <q-item-section>
            <q-item-label>Processes</q-item-label>
          </q-item-section>
        </q-item>

        <q-item
          clickable
          v-ripple
          :to="{ name: 'data' }"
        >
          <q-item-section avatar>
            <q-icon name="table_chart" />
          </q-item-section>
          <q-item-section>
            <q-item-label>Data Viewer</q-item-label>
          </q-item-section>
        </q-item>

        <q-item
          clickable
          v-ripple
          :to="{ name: 'compare' }"
        >
          <q-item-section avatar>
            <q-icon name="compare_arrows" />
          </q-item-section>
          <q-item-section>
            <q-item-label>Comparison</q-item-label>
          </q-item-section>
        </q-item>

        <q-separator spaced />

        <q-item-label header class="text-grey-8">
          Settings
        </q-item-label>

        <q-item
          clickable
          v-ripple
          :to="{ name: 'xero-connect' }"
        >
          <q-item-section avatar>
            <q-icon name="link" />
          </q-item-section>
          <q-item-section>
            <q-item-label>Connect to Xero</q-item-label>
          </q-item-section>
        </q-item>

      </q-list>
    </q-drawer>

    <q-page-container>
      <router-view />
    </q-page-container>
  </q-layout>
</template>

<script setup>
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import { useDataStore } from '../stores/data';
import TenantSelector from '../components/TenantSelector.vue';

const leftDrawerOpen = ref(false);
const router = useRouter();
const authStore = useAuthStore();
const dataStore = useDataStore();

onMounted(() => {
  // Load tenants after user is authenticated
  dataStore.loadTenants().catch(err => {
    console.warn('Failed to load tenants:', err);
  });
});

function handleLogout() {
  authStore.logout();
  router.push({ name: 'login' });
}
</script>
