<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">④ 切分预览</h2>
    <el-empty v-if="!result" description="尚未生成 chunk 结果" />
    <template v-else>
      <el-descriptions :column="4" border>
        <el-descriptions-item label="chunk 总数">{{ chunks.length }}</el-descriptions-item>
        <el-descriptions-item label="空文本">{{ stats.empty_text ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="过短">{{ stats.too_short ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="重复 hash">{{ stats.duplicate_hashes ?? 0 }}</el-descriptions-item>
      </el-descriptions>
      <el-input v-model="keyword" class="mt" placeholder="按文本/source_id 搜索" clearable />
      <el-table :data="filteredChunks" height="380" class="mt">
        <el-table-column prop="chunk_id" label="chunk_id" min-width="190" />
        <el-table-column prop="source_id" label="source_id" min-width="160" />
        <el-table-column prop="char_count" label="字符数" width="90" />
        <el-table-column prop="text" label="内容" min-width="260" show-overflow-tooltip />
      </el-table>
      <RawJsonPanel title="chunk Raw JSON" :data="result" />
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import RawJsonPanel from './RawJsonPanel.vue'

const props = defineProps<{ result?: Record<string, unknown> }>()
const keyword = ref('')
const chunks = computed<Record<string, unknown>[]>(() => (Array.isArray(props.result?.chunks) ? props.result.chunks as Record<string, unknown>[] : []))
const stats = computed<Record<string, unknown>>(() => (props.result?.stats && typeof props.result.stats === 'object' ? props.result.stats as Record<string, unknown> : {}))
const filteredChunks = computed(() => {
  const key = keyword.value.trim()
  if (!key) return chunks.value
  return chunks.value.filter((item) => JSON.stringify(item).includes(key))
})
</script>

<style scoped>
.mt {
  margin-top: 16px;
}
</style>
