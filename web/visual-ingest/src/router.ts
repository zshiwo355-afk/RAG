import { createRouter, createWebHistory } from 'vue-router'
import VisualIngest from './views/VisualIngest.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/rag/visual-ingest' },
    { path: '/rag/visual-ingest', component: VisualIngest }
  ]
})
