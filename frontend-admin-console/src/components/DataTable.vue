<template>
  <q-table
    :rows="rows"
    :columns="columns"
    :loading="loading"
    :pagination="pagination"
    row-key="id"
    @request="onRequest"
  >
    <template v-slot:body-cell-amount="props">
      <q-td :props="props">
        {{ formatCurrency(props.value) }}
      </q-td>
    </template>
  </q-table>
</template>

<script setup>
import { formatCurrency } from '../utils/helpers';

defineProps({
  rows: {
    type: Array,
    required: true,
  },
  columns: {
    type: Array,
    required: true,
  },
  loading: {
    type: Boolean,
    default: false,
  },
  pagination: {
    type: Object,
    default: () => ({
      page: 1,
      rowsPerPage: 25,
      rowsNumber: 0,
    }),
  },
});

const emit = defineEmits(['request']);

function onRequest(props) {
  emit('request', props);
}
</script>
