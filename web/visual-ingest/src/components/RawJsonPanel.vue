<template>
  <el-collapse>
    <el-collapse-item :title="title" name="raw">
      <pre class="json-box">{{ formatted }}</pre>
    </el-collapse-item>
  </el-collapse>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  title?: string
  data?: unknown
}>()

const formatted = computed(() => JSON.stringify(props.data ?? {}, null, 2))
</script>
