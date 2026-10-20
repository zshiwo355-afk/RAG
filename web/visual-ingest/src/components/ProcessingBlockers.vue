<template>
  <section v-if="blockers.length" class="processing-blockers" :class="{ compact }" aria-label="当前待处理环节">
    <h3 v-if="!compact">待处理环节与处理建议</h3>
    <ul>
      <li v-for="blocker in visibleBlockers" :key="blocker.code">
        <p class="blocker-stage">{{ blocker.stage }}</p>
        <p>{{ blocker.reason }}</p>
        <p class="blocker-action">下一步：{{ blocker.action }}</p>
      </li>
    </ul>
    <button v-if="compact && (blockers.length > 2 || job.blocking_reasons_truncated)" type="button" class="text-button" @click="$emit('details')">查看全部问题 →</button>
    <p v-else-if="job.blocking_reasons_truncated" class="blocker-action">原因较多，以下逐项处理结果可查看完整记录。</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { processingBlockers, type ProcessingJob } from '../api/knowledgePortal'
const props = defineProps<{ job: ProcessingJob; compact?: boolean }>()
defineEmits<{ details: [] }>()
const blockers = computed(() => processingBlockers(props.job))
const visibleBlockers = computed(() => props.compact ? blockers.value.slice(0, 2) : blockers.value)
</script>

<style scoped>
.processing-blockers { margin: 16px 0; padding: 16px; border: 1px solid #e3d7ba; border-radius: 4px; background: #fffcf5; font-size: 13px; line-height: 1.65; }
h3 { margin: 0 0 12px; font-size: 15px; }
ul { padding: 0; margin: 0; list-style: none; }
li + li { border-top: 1px solid #e3d7ba; margin-top: 12px; padding-top: 12px; }
p { margin: 3px 0; overflow-wrap: anywhere; }
.blocker-stage { color: #765419; font-weight: 600; }
.blocker-action { color: #68707c; }
.compact { margin: 12px 0 0; padding: 0 0 0 10px; border: 0; border-left: 2px solid #d9b76e; border-radius: 0; background: none; font-size: 12px; }
.compact li + li { border: 0; padding-top: 0; margin-top: 10px; }
.text-button { margin-top: 8px; font-size: 12px; }
</style>
