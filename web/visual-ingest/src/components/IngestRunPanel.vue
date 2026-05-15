<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">⑥ 执行入库</h2>
    <el-alert title="v1 只执行 dry-run。真实入库、embedding、push、delete 均不开放。" type="warning" :closable="false" />
    <el-button class="mt" type="primary" :disabled="blocked" @click="emit('run')">执行 dry-run</el-button>
    <el-alert v-if="blocked" class="mt" title="preflight blocked，不能继续执行。" type="error" :closable="false" />
    <RawJsonPanel title="执行结果 Raw JSON" :data="result" />
  </el-card>
</template>

<script setup lang="ts">
import RawJsonPanel from './RawJsonPanel.vue'

defineProps<{ result?: Record<string, unknown>, blocked: boolean }>()
const emit = defineEmits<{ run: [] }>()
</script>

<style scoped>
.mt {
  margin-top: 16px;
}
</style>
