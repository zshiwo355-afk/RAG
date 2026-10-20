import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/rag/visual-ingest' },
    { path: '/rag/visual-ingest', component: () => import('./views/VisualIngest.vue'), meta: { title: 'RAG 资料入库可视化控制台' } },
    { path: '/rag/knowledge', component: () => import('./views/KnowledgePortal.vue'), meta: { title: '公司知识中心 · 知识总览' } }
  ]
})

router.afterEach((to) => {
  // The knowledge portal owns the title for its independently addressable hash views.
  if (to.path !== '/rag/knowledge') document.title = String(to.meta.title || 'RAG')
})

export default router
