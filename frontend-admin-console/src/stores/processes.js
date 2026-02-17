import { defineStore } from 'pinia';
import { ref } from 'vue';
import {
  updateMetadata,
  updateData,
  processJournals,
  processTrailBalance,
  reconcileReports,
  importPnlByTracking,
} from '../api/endpoints';

export const useProcessStore = defineStore('processes', () => {
  const activeProcesses = ref(new Map());
  const processHistory = ref([]);

  function addToHistory(processType, result) {
    processHistory.value.unshift({
      type: processType,
      result,
      timestamp: new Date().toISOString(),
    });
    // Keep only last 50 entries
    if (processHistory.value.length > 50) {
      processHistory.value = processHistory.value.slice(0, 50);
    }
  }

  async function runProcess(processType, params) {
    const processId = `${processType}_${Date.now()}`;
    activeProcesses.value.set(processId, { type: processType, status: 'running' });

    try {
      let result;
      switch (processType) {
        case 'metadata':
          result = await updateMetadata(params.tenantId);
          break;
        case 'data':
          result = await updateData(params.tenantId, params.loadAll);
          break;
        case 'journals':
          result = await processJournals(params.tenantId);
          break;
        case 'trail-balance':
          result = await processTrailBalance(params.tenantId, {
            rebuild_trail_balance: params.rebuild_trail_balance,
            exclude_manual_journals: params.exclude_manual_journals,
          });
          break;
        case 'reconcile':
          result = await reconcileReports(params.tenantId, {
            financial_year: params.financial_year,
            fiscal_year_start_month: params.fiscal_year_start_month,
            tolerance: params.tolerance,
          });
          break;
        case 'pnl-by-tracking':
          result = await importPnlByTracking(params.tenantId, {
            from_date: params.from_date,
            to_date: params.to_date,
          });
          break;
        default:
          throw new Error(`Unknown process type: ${processType}`);
      }

      activeProcesses.value.set(processId, { type: processType, status: 'success', result });
      addToHistory(processType, result);
      
      // Remove from active after 5 seconds
      setTimeout(() => {
        activeProcesses.value.delete(processId);
      }, 5000);

      return { success: true, result };
    } catch (error) {
      const errorMessage = error.response?.data?.error || error.message || 'Process failed';
      activeProcesses.value.set(processId, { type: processType, status: 'error', error: errorMessage });
      addToHistory(processType, { error: errorMessage });
      
      setTimeout(() => {
        activeProcesses.value.delete(processId);
      }, 10000);

      return { success: false, error: errorMessage };
    }
  }

  function getActiveProcessesByType(type) {
    return Array.from(activeProcesses.value.values()).filter(p => p.type === type);
  }

  return {
    activeProcesses,
    processHistory,
    runProcess,
    getActiveProcessesByType,
  };
});
