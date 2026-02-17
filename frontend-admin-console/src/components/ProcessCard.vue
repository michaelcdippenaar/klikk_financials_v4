<template>
  <q-card class="q-mb-md">
    <q-card-section>
      <div class="text-h6">{{ title }}</div>
      <div class="text-caption text-grey-7">{{ description }}</div>
    </q-card-section>

    <q-card-section v-if="showForm">
      <slot name="form"></slot>
    </q-card-section>

    <q-card-section v-if="loading" class="text-center">
      <q-spinner color="primary" size="3em" />
      <div class="q-mt-md text-grey-7">Running process...</div>
    </q-card-section>

    <q-card-section v-if="result && !loading">
      <q-badge :color="result.success ? 'positive' : 'negative'" class="q-mb-sm">
        {{ result.success ? 'Success' : 'Error' }}
      </q-badge>
      <div v-if="result.success && result.data">
        <pre class="q-mt-sm" style="max-height: 300px; overflow: auto; font-size: 0.85em;">{{ JSON.stringify(result.data, null, 2) }}</pre>
      </div>
      <div v-else-if="result.error" class="text-negative">
        {{ result.error }}
      </div>
    </q-card-section>

    <q-card-actions align="right">
      <slot name="actions">
        <q-btn
          :label="buttonLabel"
          color="primary"
          :loading="loading"
          :disable="loading || !canRun"
          @click="$emit('run')"
        />
      </slot>
    </q-card-actions>
  </q-card>
</template>

<script setup>
defineProps({
  title: {
    type: String,
    required: true,
  },
  description: {
    type: String,
    default: '',
  },
  loading: {
    type: Boolean,
    default: false,
  },
  result: {
    type: Object,
    default: null,
  },
  showForm: {
    type: Boolean,
    default: false,
  },
  buttonLabel: {
    type: String,
    default: 'Run',
  },
  canRun: {
    type: Boolean,
    default: true,
  },
});

defineEmits(['run']);
</script>
