import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { login as apiLogin, refreshToken as apiRefreshToken } from '../api/auth';
import { STORAGE_KEYS } from '../utils/constants';

export const useAuthStore = defineStore('auth', () => {
  const user = ref(null);
  const token = ref(localStorage.getItem(STORAGE_KEYS.TOKEN) || null);
  const refreshTokenValue = ref(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN) || null);

  const isAuthenticated = computed(() => !!token.value);

  // Load user from localStorage on init
  const storedUser = localStorage.getItem(STORAGE_KEYS.USER);
  if (storedUser) {
    try {
      user.value = JSON.parse(storedUser);
    } catch (e) {
      console.error('Failed to parse stored user', e);
    }
  }

  async function login(username, password) {
    try {
      const data = await apiLogin(username, password);
      token.value = data.access;
      refreshTokenValue.value = data.refresh;
      user.value = data.user || { username };

      // Store in localStorage
      localStorage.setItem(STORAGE_KEYS.TOKEN, token.value);
      localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, refreshTokenValue.value);
      localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(user.value));

      return { success: true };
    } catch (error) {
      return {
        success: false,
        error: error.response?.data?.detail || error.message || 'Login failed',
      };
    }
  }

  async function refreshToken() {
    if (!refreshTokenValue.value) {
      throw new Error('No refresh token available');
    }

    try {
      const data = await apiRefreshToken(refreshTokenValue.value);
      token.value = data.access;
      localStorage.setItem(STORAGE_KEYS.TOKEN, token.value);
      return { success: true };
    } catch (error) {
      logout();
      throw error;
    }
  }

  function logout() {
    user.value = null;
    token.value = null;
    refreshTokenValue.value = null;
    localStorage.removeItem(STORAGE_KEYS.TOKEN);
    localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN);
    localStorage.removeItem(STORAGE_KEYS.USER);
    localStorage.removeItem(STORAGE_KEYS.SELECTED_TENANT);
  }

  return {
    user,
    token,
    refreshTokenValue,
    isAuthenticated,
    login,
    refreshToken,
    logout,
  };
});
