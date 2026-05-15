<template>
  <el-card class="panel" shadow="never">
    <h2 class="panel-title">① 资料输入</h2>
    <el-alert
      title="文件上传、文本和 JSON 只进入预览流程，不会直接写入向量库。"
      type="info"
      :closable="false"
      class="mb"
    />
    <el-form label-width="120px">
      <el-form-item label="输入类型">
        <el-radio-group v-model="state.sourceInput.inputType">
          <el-radio-button label="file">文件上传</el-radio-button>
          <el-radio-button label="text">文本粘贴</el-radio-button>
          <el-radio-button label="json">JSON 粘贴</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="资料来源名称">
        <el-input v-model="state.sourceInput.sourceName" placeholder="例如：新品资料、质检补充、活动资料" />
      </el-form-item>
      <el-form-item label="source_id">
        <el-input v-model="state.sourceInput.sourceId" placeholder="可留空，由后端根据内容生成" />
      </el-form-item>
      <el-form-item v-if="state.sourceInput.inputType === 'file'" label="上传文件">
        <el-upload :auto-upload="false" :limit="1" :on-change="onFileChange" :on-remove="onFileRemove">
          <el-button type="primary">选择文件</el-button>
          <template #tip>
            <div class="muted">支持 txt / md / json / csv / xlsx / xls / pdf。保存后仅用于解析预览。</div>
          </template>
        </el-upload>
        <el-button class="upload-button" :disabled="!selectedFile" @click="emit('upload-file', selectedFile)">保存上传文件</el-button>
      </el-form-item>
      <el-form-item v-if="state.sourceInput.inputType === 'text'" label="文本内容">
        <el-input v-model="state.sourceInput.rawText" type="textarea" :rows="10" placeholder="粘贴资料正文" />
      </el-form-item>
      <el-form-item v-if="state.sourceInput.inputType === 'json'" label="JSON 内容">
        <el-input v-model="state.sourceInput.rawText" type="textarea" :rows="10" placeholder='{"title":"标题","content":"正文"}' />
      </el-form-item>
    </el-form>
    <RawJsonPanel title="上传结果 / 当前输入 Raw JSON" :data="{ uploadResult: state.uploadResult, sourceInput: state.sourceInput }" />
  </el-card>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import RawJsonPanel from './RawJsonPanel.vue'
import type { VisualIngestState } from '../types/ragVisualIngest'

defineProps<{ state: VisualIngestState }>()
const emit = defineEmits<{ 'upload-file': [file: File | null] }>()
const selectedFile = ref<File | null>(null)

function onFileChange(file: { raw?: File }) {
  selectedFile.value = file.raw ?? null
}

function onFileRemove() {
  selectedFile.value = null
}
</script>

<style scoped>
.mb {
  margin-bottom: 16px;
}
.upload-button {
  margin-left: 12px;
}
</style>
