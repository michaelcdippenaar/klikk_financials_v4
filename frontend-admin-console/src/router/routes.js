const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('pages/Login.vue'),
  },
  {
    path: '/',
    component: () => import('layouts/MainLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        name: 'dashboard',
        component: () => import('pages/Dashboard.vue'),
      },
      {
        path: 'processes',
        name: 'processes',
        component: () => import('pages/Processes.vue'),
      },
      {
        path: 'data',
        name: 'data',
        component: () => import('pages/DataViewer.vue'),
      },
      {
        path: 'compare',
        name: 'compare',
        component: () => import('pages/Comparison.vue'),
      },
      {
        path: 'investec',
        redirect: { name: 'investec-holdings' },
      },
      {
        path: 'investec/holdings',
        name: 'investec-holdings',
        component: () => import('pages/InvestecHoldings.vue'),
      },
      {
        path: 'investec/transactions',
        name: 'investec-transactions',
        component: () => import('pages/InvestecTransactions.vue'),
      },
      {
        path: 'investec/share-codes',
        name: 'investec-share-codes',
        component: () => import('pages/InvestecShareCodes.vue'),
      },
      {
        path: 'xero-connect',
        name: 'xero-connect',
        component: () => import('pages/XeroConnect.vue'),
      },
    ],
  },
  {
    path: '/:catchAll(.*)*',
    component: () => import('pages/Error404.vue'),
  },
];

export default routes;
