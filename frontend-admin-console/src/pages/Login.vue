<template>
  <div class="fullscreen flex flex-center bg-grey-1">
    <q-card style="min-width: 350px">
      <q-card-section>
        <div class="text-h6 text-center">Klikk Admin Console</div>
        <div class="text-subtitle2 text-center text-grey-7 q-mt-sm">Sign in to continue</div>
      </q-card-section>

      <q-card-section>
        <q-form @submit="handleLogin" class="q-gutter-md">
          <q-input
            v-model="username"
            label="Username"
            outlined
            dense
            :rules="[val => !!val || 'Username is required']"
          />

          <q-input
            v-model="password"
            label="Password"
            type="password"
            outlined
            dense
            :rules="[val => !!val || 'Password is required']"
          />

          <div v-if="error" class="text-negative text-caption q-mt-sm">
            {{ error }}
          </div>

          <div>
            <q-btn
              label="Login"
              type="submit"
              color="primary"
              class="full-width"
              :loading="loading"
            />
          </div>
        </q-form>
      </q-card-section>
    </q-card>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { useAuthStore } from '../stores/auth';

const router = useRouter();
const route = useRoute();
const authStore = useAuthStore();

const username = ref('');
const password = ref('');
const loading = ref(false);
const error = ref('');

async function handleLogin() {
  error.value = '';
  loading.value = true;

  try {
    const result = await authStore.login(username.value, password.value);
    
    if (result.success) {
      const redirect = route.query.redirect || '/';
      router.push(redirect);
    } else {
      error.value = result.error || 'Login failed';
    }
  } catch (err) {
    error.value = err.message || 'An error occurred during login';
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.full-height {
  min-height: 100vh;
}
</style>
