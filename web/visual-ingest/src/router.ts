import { createRouter, createWebHistory } from 'vue-router'
import VisualIngest from './views/VisualIngest.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/rag/visual-ingest' },
    { path: '/rag/visual-ingest', component: VisualIngest, meta: { title: 'RAG 资料入库可视化控制台' } },
    { path: '/rag/knowledge', component: () => import('./views/KnowledgePortal.vue'), meta: { title: '公司知识中心 · 知识总览' } }
  ]
})

router.afterEach((to) => { document.title = String(to.meta.title || 'RAG') })

export default router
