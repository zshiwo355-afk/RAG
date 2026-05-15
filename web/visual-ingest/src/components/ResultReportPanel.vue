<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">⑧ 结果报告</h2>
    <el-button type="primary" @click="copyReport">复制报告</el-button>
    <el-input class="mt" :model-value="report" type="textarea" :rows="18" readonly />
  </el-card>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus'

const props = defineProps<{ report: string }>()

async function copyReport() {
  await navigator.clipboard.writeText(props.report)
  ElMessage.success('报告已复制')
}
</script>

<style scoped>
.mt {
  margin-top: 16px;
}
</style>
