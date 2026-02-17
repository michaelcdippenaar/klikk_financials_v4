<template>
  <q-page class="q-pa-md">
    <div class="row q-mb-md">
      <div class="col">
        <div class="text-h4">Welcome back</div>
        <div class="text-subtitle1 text-grey-7" v-if="dataStore.selectedTenantName">
          {{ dataStore.selectedTenantName }}
        </div>
      </div>
    </div>

    <div v-if="!dataStore.selectedTenant" class="q-pa-lg text-center">
      <q-icon name="info" size="3em" color="grey-5" />
      <div class="text-h6 q-mt-md text-grey-7">Please select a tenant to continue</div>
    </div>

    <div v-else>
      <!-- Summary Cards -->
      <div class="row q-gutter-md q-mb-md">
        <q-card class="col-12 col-sm-6 col-md-3">
          <q-card-section>
            <div class="text-grey-7 text-caption">Accounts</div>
            <div class="text-h5">{{ summary?.accounts_count || 0 }}</div>
          </q-card-section>
        </q-card>

        <q-card class="col-12 col-sm-6 col-md-3">
          <q-card-section>
            <div class="text-grey-7 text-caption">Journals</div>
            <div class="text-h5">{{ summary?.journals_count || 0 }}</div>
          </q-card-section>
        </q-card>

        <q-card class="col-12 col-sm-6 col-md-3">
          <q-card-section>
            <div class="text-grey-7 text-caption">Trail Balance</div>
            <div class="text-h5">{{ summary?.trail_balance_count || 0 }}</div>
          </q-card-section>
        </q-card>

        <q-card class="col-12 col-sm-6 col-md-3">
          <q-card-section>
            <div class="text-grey-7 text-caption">Balance Sheet</div>
            <div class="text-h5">{{ summary?.balance_sheet_count || 0 }}</div>
          </q-card-section>
        </q-card>
      </div>

      <!-- Quick Actions -->
      <q-card>
        <q-card-section>
          <div class="text-h6">Quick Actions</div>
        </q-card-section>
        <q-card-section>
          <div class="row q-gutter-md">
            <q-btn
              label="Update Metadata"
              color="primary"
              outline
              @click="$router.push({ name: 'processes' })"
            />
            <q-btn
              label="Sync Data"
              color="primary"
              outline
              @click="$router.push({ name: 'processes' })"
            />
            <q-btn
              label="View Trail Balance"
              color="primary"
              outline
              @click="$router.push({ name: 'data' })"
            />
            <q-btn
              label="Run Reconciliation"
              color="primary"
              outline
              @click="$router.push({ name: 'compare' })"
            />
          </div>
        </q-card-section>
      </q-card>

      <!-- Recent Activity -->
      <q-card class="q-mt-md" v-if="processStore.processHistory.length > 0">
        <q-card-section>
          <div class="text-h6">Recent Activity</div>
        </q-card-section>
        <q-card-section>
          <q-list>
            <q-item
              v-for="(item, index) in processStore.processHistory.slice(0, 5)"
              :key="index"
            >
              <q-item-section avatar>
                <q-icon
                  :name="item.result?.error ? 'error' : 'check_circle'"
                  :color="item.result?.error ? 'negative' : 'positive'"
                />
              </q-item-section>
              <q-item-section>
                <q-item-label>{{ item.type }}</q-item-label>
                <q-item-label caption>{{ formatDate(item.timestamp) }}</q-item-label>
              </q-item-section>
            </q-item>
          </q-list>
        </q-card-section>
      </q-card>
    </div>
  </q-page>
</template>

<script setup>
import { onMounted, computed } from 'vue';
import { useDataStore } from '../stores/data';
import { useProcessStore } from '../stores/processes';
import { formatDate } from '../utils/helpers';

const dataStore = useDataStore();
const processStore = useProcessStore();

const summary = computed(() => dataStore.summary);

onMounted(async () => {
  if (dataStore.selectedTenant) {
    await dataStore.fetchSummary();
  }
});
</script>
