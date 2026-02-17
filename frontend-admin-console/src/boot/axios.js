import { boot } from 'quasar/wrappers';
import apiClient from '../api/client';

export default boot(({ app }) => {
  // Make apiClient available globally
  app.config.globalProperties.$api = apiClient;
});
