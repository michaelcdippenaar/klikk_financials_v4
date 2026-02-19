import { route } from 'quasar/wrappers';
import { createRouter, createMemoryHistory, createWebHashHistory, createWebHistory } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import routes from './routes';

/*
 * If not building with SSR mode, you can
 * directly export the Router instantiation;
 *
 * The function below can be async too; either use
 * async/await or return a Promise which resolves
 * with the Router instance.
 */

export default route(function (/* { store, ssrContext } */) {
  const createHistory = process.env.SERVER
    ? createMemoryHistory
    : createWebHistory;

  const Router = createRouter({
    scrollBehavior: () => ({ left: 0, top: 0 }),
    routes,
    history: createHistory(process.env.VUE_ROUTER_BASE),
  });

  // Navigation guard for protected routes
  Router.beforeEach((to, from, next) => {
    try {
      const authStore = useAuthStore();

      // Require login for protected routes
      if (to.meta.requiresAuth && !authStore.isAuthenticated) {
        next({ name: 'login', query: { redirect: to.fullPath } });
        return;
      }

      // Redirect authenticated users away from login page
      if (to.path === '/login' && authStore.isAuthenticated) {
        next({ name: 'dashboard' });
        return;
      }

      next();
    } catch (error) {
      console.error('Router guard error:', error);
      next();
    }
  });

  return Router;
});
