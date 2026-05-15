<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">⑦ 检索验证</h2>
    <el-alert title="默认只调用 search，不调用 answer，不生成 LLM 回答。" type="info" :closable="false" />
    <el-form label-width="120px" class="mt">
      <el-form-item label="测试 query">
        <el-input v-model="query" placeholder="输入检索问题" />
      </el-form-item>
      <el-form-item label="top_k">
        <el-input-number v-model="topK" :min="1" :max="30" />
      </el-form-item>
      <el-form-item label="answer 验证">
        <el-switch v-model="answerEnabled" />
        <span class="muted switch-note">开启后会调用 /api/rag/answer 和 LLM，默认关闭。</span>
      </el-form-item>
      <el-form-item v-if="answerEnabled" label="确认口令">
        <el-input v-model="answerToken" placeholder="YES_ANSWER" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="emit('search', query, topK)">执行 search 验证</el-button>
        <el-button :disabled="!answerEnabled || answerToken !== 'YES_ANSWER'" type="warning" @click="emit('answer', query, topK, answerToken)">
          调用 answer 验证
        </el-button>
      </el-form-item>
    </el-form>
    <el-tabs class="mt">
      <el-tab-pane label="产品结果">
        <el-table :data="products" height="320">
          <el-table-column prop="product_id" label="product_id" min-width="180" />
          <el-table-column prop="final_score" label="score" width="110" />
          <el-table-column prop="best_text" label="摘要" min-width="260" show-overflow-tooltip />
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="product_bundles">
        <el-table :data="bundles" height="320">
          <el-table-column prop="product_id" label="product_id" min-width="180" />
          <el-table-column label="supplements" width="120">
            <template #default="{ row }">{{ count(row.supplements) }}</template>
          </el-table-column>
          <el-table-column label="quality_reports" width="140">
            <template #default="{ row }">{{ count(row.quality_reports) }}</template>
          </el-table-column>
          <el-table-column label="conflicts" width="110">
            <template #default="{ row }">{{ count(row.conflicts) }}</template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="Raw JSON">
        <pre class="json-box">{{ JSON.stringify(result ?? {}, null, 2) }}</pre>
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ result?: Record<string, unknown> }>()
const emit = defineEmits<{
  search: [query: string, topK: number]
  answer: [query: string, topK: number, confirmation: string]
}>()
const query = ref('')
const topK = ref(5)
const answerEnabled = ref(false)
const answerToken = ref('')
const products = computed(() => (Array.isArray(props.result?.products) ? props.result.products : []))
const bundles = computed(() => (Array.isArray(props.result?.product_bundles) ? props.result.product_bundles : []))
function count(value: unknown) {
  return Array.isArray(value) ? value.length : 0
}
</script>

<style scoped>
.mt {
  margin-top: 16px;
}
.switch-note {
  margin-left: 12px;
}
</style>
