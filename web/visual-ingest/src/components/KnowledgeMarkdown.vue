<template>
  <article class="knowledge-markdown" aria-label="文档正文" v-html="html" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderKnowledgeMarkdown } from '../knowledge-markdown'

const props = defineProps<{ content: string }>()
const html = computed(() => renderKnowledgeMarkdown(props.content))
</script>

<style scoped>
.knowledge-markdown { color: #252b34; font-size: 15px; line-height: 1.85; overflow-wrap: anywhere; }
.knowledge-markdown :deep(> :first-child) { margin-top: 0; }
.knowledge-markdown :deep(h1), .knowledge-markdown :deep(h2), .knowledge-markdown :deep(h3),
.knowledge-markdown :deep(h4), .knowledge-markdown :deep(h5), .knowledge-markdown :deep(h6) { color: #171c24; font-weight: 650; line-height: 1.5; margin: 1.8em 0 .7em; }
.knowledge-markdown :deep(h1) { font-size: 26px; }
.knowledge-markdown :deep(h2) { font-size: 21px; padding-bottom: .5em; border-bottom: 1px solid #e5e9ef; }
.knowledge-markdown :deep(h3) { font-size: 18px; }
.knowledge-markdown :deep(h4), .knowledge-markdown :deep(h5), .knowledge-markdown :deep(h6) { font-size: 16px; }
.knowledge-markdown :deep(p) { margin: .8em 0; }
.knowledge-markdown :deep(ul), .knowledge-markdown :deep(ol) { margin: .8em 0; padding-left: 1.65em; }
.knowledge-markdown :deep(li) { margin: .3em 0; }
.knowledge-markdown :deep(li > p) { margin: .3em 0; }
.knowledge-markdown :deep(strong) { color: #171c24; font-weight: 650; }
.knowledge-markdown :deep(a) { color: #285acb; text-decoration: underline; text-underline-offset: 3px; }
.knowledge-markdown :deep(a:focus-visible), .knowledge-markdown :deep(.markdown-table-wrap:focus-visible) { outline: 2px solid #285acb; outline-offset: 3px; }
.knowledge-markdown :deep(blockquote) { margin: 1em 0; padding: .2em 1.2em; border-left: 3px solid #c7d3e9; background: #f7f9fc; color: #536174; }
.knowledge-markdown :deep(hr) { border: 0; border-top: 1px solid #e5e9ef; margin: 1.8em 0; }
.knowledge-markdown :deep(code) { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .9em; background: #f2f4f7; border-radius: 3px; padding: .12em .35em; }
.knowledge-markdown :deep(pre) { overflow-x: auto; padding: 16px; border: 1px solid #e5e9ef; border-radius: 6px; background: #f7f9fc; line-height: 1.65; white-space: pre; }
.knowledge-markdown :deep(pre code) { padding: 0; background: none; }
.knowledge-markdown :deep(.markdown-table-wrap) { max-width: 100%; overflow-x: auto; margin: 1.2em 0; border: 1px solid #dfe5ed; border-radius: 6px; }
.knowledge-markdown :deep(table) { width: 100%; border-collapse: collapse; font-size: 14px; line-height: 1.7; }
.knowledge-markdown :deep(th), .knowledge-markdown :deep(td) { min-width: 100px; padding: 11px 14px; border-right: 1px solid #e5e9ef; border-bottom: 1px solid #e5e9ef; text-align: left; vertical-align: top; }
.knowledge-markdown :deep(th) { background: #f5f7fa; color: #202a38; font-weight: 600; }
.knowledge-markdown :deep(tr:last-child td) { border-bottom: 0; }
.knowledge-markdown :deep(th:last-child), .knowledge-markdown :deep(td:last-child) { border-right: 0; }
.knowledge-markdown :deep(.markdown-align-center) { text-align: center; }
.knowledge-markdown :deep(.markdown-align-right) { text-align: right; }
.knowledge-markdown :deep(.markdown-image-note) { color: #667085; }
@media (max-width: 600px) {
  .knowledge-markdown { font-size: 14px; }
  .knowledge-markdown :deep(h1) { font-size: 23px; }
  .knowledge-markdown :deep(h2) { font-size: 19px; }
  .knowledge-markdown :deep(th), .knowledge-markdown :deep(td) { padding: 9px 11px; }
}
</style>
