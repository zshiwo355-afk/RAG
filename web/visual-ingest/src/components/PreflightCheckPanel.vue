<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">⑤ 入库预检</h2>
    <el-empty v-if="!result" description="尚未生成预检结果" />
    <template v-else>
      <el-alert :title="`预检状态：${result.status}`" :type="alertType" :closable="false" />
      <el-table :data="checks" height="360" class="mt">
        <el-table-column prop="name" label="检查项" width="150" />
        <el-table-column prop="status" label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="tagType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="说明" min-width="260" />
      </el-table>
      <div class="safe-note mt">
        embedding={{ result.will_call_embedding }}，写库={{ result.will_write_vector_db }}，push={{ result.will_push }}，delete={{ result.will_delete }}
      </div>
      <RawJsonPanel title="preflight Raw JSON" :data="result" />
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import RawJsonPanel from './RawJsonPanel.vue'

const props = defineProps<{ result?: Record<string, unknown> }>()
const checks = computed(() => (Array.isArray(props.result?.checks) ? props.result.checks : []))
const alertType = computed(() => props.result?.status === 'blocked' ? 'error' : props.result?.status === 'warning' ? 'warning' : 'success')
function tagType(status: string) {
  if (status === 'blocked') return 'danger'
  if (status === 'warning') return 'warning'
  return 'success'
}
</script>

<style scoped>
.mt {
  margin-top: 16px;
}
</style>
