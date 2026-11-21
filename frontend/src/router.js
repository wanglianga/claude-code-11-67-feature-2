import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('./views/LoginView.vue'), meta: { public: true } },
  { path: '/', redirect: '/catalog' },
  { path: '/catalog', component: () => import('./views/CatalogView.vue'), meta: { title: '产品目录' } },
  { path: '/projects', component: () => import('./views/ProjectsView.vue'), meta: { title: '课题档案' } },
  { path: '/workbench', component: () => import('./views/WorkbenchView.vue'), meta: { title: '资格开通' } },
  { path: '/access', component: () => import('./views/AccessView.vue'), meta: { title: '数据访问' } },
  { path: '/quota', component: () => import('./views/QuotaView.vue'), meta: { title: '额度账本' } },
  { path: '/estimator', component: () => import('./views/EstimatorView.vue'), meta: { title: '资源成本估计' } },
  { path: '/audit', component: () => import('./views/AuditView.vue'), meta: { title: '审计查询' } },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (!to.meta.public && !localStorage.getItem('token')) return '/login'
})

export default router
