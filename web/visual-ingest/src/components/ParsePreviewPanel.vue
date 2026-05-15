<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">③ 解析预览</h2>
    <el-empty v-if="!result" description="尚未生成解析结果" />
    <template v-else>
      <el-descriptions :column="3" border>
        <el-descriptions-item label="文档数量">{{ documents.length }}</el-descriptions-item>
        <el-descriptions-item label="错误数量">{{ errors.length }}</el-descriptions-item>
        <el-descriptions-item label="Warning 数量">{{ warnings.length }}</el-descriptions-item>
      </el-descriptions>
      <el-table :data="documents" height="340" class="mt">
        <el-table-column prop="document_id" label="document_id" width="130" />
        <el-table-column prop="title" label="标题" min-width="180" />
        <el-table-column prop="source_id" label="source_id" min-width="180" />
        <el-table-column prop="text_length" label="文本长度" width="110" />
      </el-table>
      <RawJsonPanel title="解析结果 Raw JSON" :data="result" />
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import RawJsonPanel from './RawJsonPanel.vue'

const props = defineProps<{ result?: Record<string, unknown> }>()
const documents = computed(() => (Array.isArray(props.result?.documents) ? props.result.documents : []))
const warnings = computed(() => (Array.isArray(props.result?.warnings) ? props.result.warnings : []))
const errors = computed(() => (Array.isArray(props.result?.errors) ? props.result.errors : []))
</script>

<style scoped>
.mt {
  margin-top: 16px;
}
</style>
