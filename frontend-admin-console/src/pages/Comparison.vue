<template>
  <q-page class="q-pa-md">
    <div class="text-h5 q-mb-md">Report Comparison</div>

    <div v-if="!dataStore.selectedTenant" class="q-pa-lg text-center">
      <q-icon name="info" size="3em" color="grey-5" />
      <div class="text-h6 q-mt-md text-grey-7">Please select a tenant first</div>
    </div>

    <div v-else>
      <!-- Reconciliation Form -->
      <q-card class="q-mb-md">
        <q-card-section>
          <div class="text-h6">Run Reconciliation</div>
        </q-card-section>
        <q-card-section>
          <div class="row q-gutter-md">
            <q-input
              v-model.number="reconcileForm.financialYear"
              label="Financial Year"
              type="number"
              outlined
              dense
              style="min-width: 150px"
            />
            <q-input
              v-model.number="reconcileForm.fiscalYearStartMonth"
              label="Fiscal Year Start Month"
              type="number"
              min="1"
              max="12"
              outlined
              dense
              style="min-width: 180px"
            />
            <q-input
              v-model.number="reconcileForm.tolerance"
              label="Tolerance"
              type="number"
              step="0.01"
              outlined
              dense
              style="min-width: 120px"
            />
            <q-btn
              label="Run Reconciliation"
              color="primary"
              :loading="loading"
              @click="runReconciliation"
            />
          </div>
        </q-card-section>
      </q-card>

      <!-- Reconciliation Results -->
      <div v-if="reconciliationResult">
        <!-- P&L Section -->
        <q-card v-if="reconciliationResult.profit_loss" class="q-mb-md">
          <q-card-section>
            <div class="text-h6">Profit & Loss</div>
          </q-card-section>
          <q-card-section>
            <div v-if="reconciliationResult.profit_loss.import">
              <div class="text-subtitle2 q-mb-sm">Import</div>
              <pre style="background: #f5f5f5; padding: 1em; border-radius: 4px; overflow: auto;">{{ JSON.stringify(reconciliationResult.profit_loss.import, null, 2) }}</pre>
            </div>
            <div v-if="reconciliationResult.profit_loss.comparison" class="q-mt-md">
              <div class="text-subtitle2 q-mb-sm">Comparison</div>
              <pre style="background: #f5f5f5; padding: 1em; border-radius: 4px; overflow: auto;">{{ JSON.stringify(reconciliationResult.profit_loss.comparison, null, 2) }}</pre>
            </div>
          </q-card-section>
        </q-card>

        <!-- Balance Sheet Section -->
        <q-card v-if="reconciliationResult.balance_sheet" class="q-mb-md">
          <q-card-section>
            <div class="text-h6">Balance Sheet</div>
          </q-card-section>
          <q-card-section>
            <div v-if="reconciliationResult.balance_sheet.import">
              <div class="text-subtitle2 q-mb-sm">Import</div>
              <pre style="background: #f5f5f5; padding: 1em; border-radius: 4px; overflow: auto;">{{ JSON.stringify(reconciliationResult.balance_sheet.import, null, 2) }}</pre>
            </div>
            <div v-if="reconciliationResult.balance_sheet.validation" class="q-mt-md">
              <div class="text-subtitle2 q-mb-sm">Validation</div>
              <div v-if="reconciliationResult.balance_sheet.validation.match_percentage !== undefined">
                <q-badge
                  :color="reconciliationResult.balance_sheet.validation.match_percentage >= 95 ? 'positive' : 'warning'"
                  class="q-mb-sm"
                >
                  Match: {{ reconciliationResult.balance_sheet.validation.match_percentage }}%
                </q-badge>
                <div class="text-caption q-mb-sm">
                  Matches: {{ reconciliationResult.balance_sheet.validation.matches }} | 
                  Mismatches: {{ reconciliationResult.balance_sheet.validation.mismatches }}
                </div>
              </div>
              <pre style="background: #f5f5f5; padding: 1em; border-radius: 4px; overflow: auto; max-height: 400px;">{{ JSON.stringify(reconciliationResult.balance_sheet.validation, null, 2) }}</pre>
            </div>
          </q-card-section>
        </q-card>

        <!-- Overall Status -->
        <q-card>
          <q-card-section>
            <div class="text-h6">Overall Status</div>
          </q-card-section>
          <q-card-section>
            <q-badge
              :color="reconciliationResult.success ? 'positive' : 'negative'"
              class="q-mr-sm"
            >
              {{ reconciliationResult.success ? 'Success' : 'Failed' }}
            </q-badge>
            <div v-if="reconciliationResult.errors && reconciliationResult.errors.length > 0" class="q-mt-md">
              <div class="text-negative text-subtitle2">Errors:</div>
              <ul>
                <li v-for="(error, index) in reconciliationResult.errors" :key="index">{{ error }}</li>
              </ul>
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>
  </q-page>
</template>

<script setup>
import { ref, reactive } from 'vue';
import { useDataStore } from '../stores/data';
import { useProcessStore } from '../stores/processes';

const dataStore = useDataStore();
const processStore = useProcessStore();

const loading = ref(false);
const reconciliationResult = ref(null);

const reconcileForm = reactive({
  financialYear: new Date().getFullYear(),
  fiscalYearStartMonth: 7,
  tolerance: 0.01,
});

async function runReconciliation() {
  loading.value = true;
  reconciliationResult.value = null;
  try {
    const result = await processStore.runProcess('reconcile', {
      tenantId: dataStore.selectedTenant,
      financial_year: reconcileForm.financialYear,
      fiscal_year_start_month: reconcileForm.fiscalYearStartMonth,
      tolerance: reconcileForm.tolerance,
    });
    if (result.success) {
      reconciliationResult.value = result.result;
    } else {
      reconciliationResult.value = { success: false, errors: [result.error] };
    }
  } catch (error) {
    reconciliationResult.value = {
      success: false,
      errors: [error.message || 'Reconciliation failed'],
    };
  } finally {
    loading.value = false;
  }
}
</script>
