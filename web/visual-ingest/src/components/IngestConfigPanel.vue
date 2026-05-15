<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">② 入库配置</h2>
    <el-alert title="v1 只开放 dry-run / 本地预览。real-run 会被后端 blocked。" type="warning" :closable="false" class="mb" />
    <el-form label-width="140px">
      <el-form-item label="知识操作">
        <el-radio-group v-model="state.config.knowledgeAction">
          <el-radio-button label="supplement_existing">补入已有知识库</el-radio-button>
          <el-radio-button label="create_new_knowledge">新增知识</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="导入模式">
        <el-select v-model="state.config.importMode">
          <el-option label="supplement" value="supplement" />
          <el-option label="append" value="append" />
          <el-option label="partial" value="partial" />
          <el-option label="full" value="full" />
          <el-option label="overwrite" value="overwrite" />
        </el-select>
      </el-form-item>
      <el-form-item label="运行模式">
        <el-radio-group v-model="state.config.runMode">
          <el-radio-button label="dry_run">dry-run</el-radio-button>
          <el-radio-button label="real_run" disabled>real-run（v1 禁用）</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="目标 index">
        <el-input v-model="state.config.target.index" />
      </el-form-item>
      <el-form-item label="目标 collection">
        <el-input v-model="state.config.target.collection" />
      </el-form-item>
      <el-form-item label="namespace">
        <el-input v-model="state.config.target.namespace" />
      </el-form-item>
      <el-form-item label="chunk size">
        <el-input-number v-model="state.config.chunkSize" :min="100" :max="3000" />
      </el-form-item>
      <el-form-item label="overlap">
        <el-input-number v-model="state.config.overlap" :min="0" :max="1000" />
      </el-form-item>
      <el-form-item label="允许覆盖">
        <el-switch v-model="state.config.overwrite" />
        <span class="muted switch-note">v1 只提示风险，不执行覆盖。</span>
      </el-form-item>
      <template v-if="state.config.knowledgeAction === 'create_new_knowledge'">
        <el-form-item label="新知识名称">
          <el-input v-model="state.config.newKnowledgeName" />
        </el-form-item>
        <el-form-item label="新知识描述">
          <el-input v-model="state.config.newKnowledgeDescription" type="textarea" :rows="3" />
        </el-form-item>
      </template>
    </el-form>
    <RawJsonPanel title="配置 Raw JSON" :data="state.config" />
  </el-card>
</template>

<script setup lang="ts">
import RawJsonPanel from './RawJsonPanel.vue'
import type { VisualIngestState } from '../types/ragVisualIngest'

defineProps<{ state: VisualIngestState }>()
</script>

<style scoped>
.mb {
  margin-bottom: 16px;
}
.switch-note {
  margin-left: 12px;
}
</style>
