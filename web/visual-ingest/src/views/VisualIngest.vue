<template>
  <div class="page">
    <section class="hero">
      <h1>RAG 资料入库可视化控制台</h1>
      <p>上传或输入资料，按步骤完成解析、切分、预检、dry-run 和检索验证。v1 不执行真实入库。</p>
    </section>

    <el-row :gutter="16">
      <el-col :span="18">
        <el-card class="panel" shadow="never">
          <el-steps :active="state.currentStep" finish-status="success" align-center>
            <el-step v-for="item in steps" :key="item" :title="item" />
          </el-steps>
          <div class="safe-note step-summary">
            默认 dry-run；默认不调用 answer；v1 不开放 embedding / push / delete / reindex。
          </div>
        </el-card>

        <SourceInputPanel v-if="state.currentStep === 0" :state="state" @upload-file="handleUpload" />
        <IngestConfigPanel v-if="state.currentStep === 1" :state="state" />
        <ParsePreviewPanel v-if="state.currentStep === 2" :result="state.parseResult" />
        <ChunkPreviewPanel v-if="state.currentStep === 3" :result="state.chunkResult" />
        <PreflightCheckPanel v-if="state.currentStep === 4" :result="state.preflightResult" />
        <IngestRunPanel v-if="state.currentStep === 5" :result="state.ingestResult" :blocked="isPreflightBlocked" @run="handleRun" />
        <SearchVerifyPanel
          v-if="state.currentStep === 6"
          :result="state.searchVerifyResult"
          @search="handleSearch"
          @answer="handleAnswer"
        />
        <ResultReportPanel v-if="state.currentStep === 7" :report="reportText" />

        <div class="step-actions">
          <el-button :disabled="state.currentStep === 0" @click="state.currentStep--">上一步</el-button>
          <el-button @click="executeCurrentStep">执行当前步骤</el-button>
          <el-button type="primary" :disabled="!canGoNext" @click="nextStep">下一步</el-button>
        </div>
      </el-col>

      <el-col :span="6">
        <el-card class="panel" shadow="never">
          <h2 class="panel-title">运行状态</h2>
          <el-switch v-model="state.useMock" active-text="Mock 模式" inactive-text="真实接口" />
          <el-divider />
          <el-descriptions :column="1" border>
            <el-descriptions-item label="batch_id">{{ state.batchId || '未生成' }}</el-descriptions-item>
            <el-descriptions-item label="运行模式">{{ state.config.runMode }}</el-descriptions-item>
            <el-descriptions-item label="知识操作">{{ state.config.knowledgeAction }}</el-descriptions-item>
            <el-descriptions-item label="导入模式">{{ state.config.importMode }}</el-descriptions-item>
            <el-descriptions-item label="目标库">{{ state.config.target.index }}</el-descriptions-item>
          </el-descriptions>
          <el-divider />
          <div class="danger-note">
            页面不暴露 force/delete/push/reindex。real-run 在 v1 后端会被 blocked。
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import SourceInputPanel from '../components/SourceInputPanel.vue'
import IngestConfigPanel from '../components/IngestConfigPanel.vue'
import ParsePreviewPanel from '../components/ParsePreviewPanel.vue'
import ChunkPreviewPanel from '../components/ChunkPreviewPanel.vue'
import PreflightCheckPanel from '../components/PreflightCheckPanel.vue'
import IngestRunPanel from '../components/IngestRunPanel.vue'
import SearchVerifyPanel from '../components/SearchVerifyPanel.vue'
import ResultReportPanel from '../components/ResultReportPanel.vue'
import type { VisualIngestState } from '../types/ragVisualIngest'
import {
  answerVerify,
  parseSource,
  preflightIngest,
  previewChunks,
  runIngest,
  searchVerify,
  uploadFile
} from '../api/ragVisualIngest'
import {
  mockChunkResult,
  mockParseResult,
  mockPreflightResult,
  mockRunResult,
  mockSearchResult
} from '../mock/visualIngestMock'

const steps = ['资料输入', '入库配置', '解析预览', '切分预览', '入库预检', '执行入库', '检索验证', '结果报告']

const state = reactive<VisualIngestState>({
  currentStep: 0,
  batchId: '',
  useMock: true,
  sourceInput: {
    inputType: 'file',
    rawText: '',
    sourceName: '',
    sourceId: '',
    metadata: {}
  },
  config: {
    knowledgeAction: 'supplement_existing',
    importMode: 'supplement',
    runMode: 'dry_run',
    target: {
      index: 'text_docs',
      collection: 'text_docs',
      namespace: 'default'
    },
    chunkSize: 900,
    overlap: 120,
    overwrite: false,
    answerVerify: false,
    newKnowledgeName: '',
    newKnowledgeDescription: '',
    tags: [],
    version: ''
  },
  report: '',
  errors: [],
  warnings: []
})

const isPreflightBlocked = computed(() => state.preflightResult?.status === 'blocked')
const canGoNext = computed(() => {
  if (state.currentStep === 0) return hasSourceInput.value
  if (state.currentStep === 2) return Boolean(state.parseResult) && !hasParseErrors.value
  if (state.currentStep === 3) return Boolean(state.chunkResult) && chunkCount.value > 0
  if (state.currentStep === 4) return Boolean(state.preflightResult) && !isPreflightBlocked.value
  if (state.currentStep === 7) return false
  return true
})
const hasSourceInput = computed(() => {
  if (state.sourceInput.inputType === 'file') return Boolean(state.sourceInput.filePath)
  return Boolean(state.sourceInput.rawText.trim())
})
const hasParseErrors = computed(() => {
  const errors = state.parseResult?.errors
  return Array.isArray(errors) && errors.length > 0
})
const chunkCount = computed(() => Number(state.chunkResult?.chunk_count ?? 0))
const reportText = computed(() => buildReport())

function ensureBatchId() {
  if (!state.batchId) {
    state.batchId = `visual_${Date.now()}_${Math.floor(Math.random() * 10000)}`
  }
}

async function handleUpload(file: File | null) {
  if (!file) {
    ElMessage.warning('请先选择文件')
    return
  }
  ensureBatchId()
  if (state.useMock) {
    state.uploadResult = { batch_id: state.batchId, filename: file.name, path: `mock/${file.name}`, size: file.size }
    state.sourceInput.filePath = `mock/${file.name}`
    ElMessage.success('Mock 上传完成')
    return
  }
  const result = await uploadFile(file, state.batchId)
  state.uploadResult = result
  state.batchId = String(result.batch_id ?? state.batchId)
  state.sourceInput.filePath = String(result.path ?? '')
  ElMessage.success('上传已保存')
}

function normalizeJsonInput() {
  if (state.sourceInput.inputType !== 'json') return
  try {
    state.sourceInput.rawJson = JSON.parse(state.sourceInput.rawText)
  } catch {
    state.sourceInput.rawJson = undefined
  }
}

async function executeCurrentStep() {
  try {
    if (state.currentStep === 2) await handleParse()
    else if (state.currentStep === 3) await handleChunks()
    else if (state.currentStep === 4) await handlePreflight()
    else if (state.currentStep === 5) await handleRun()
    else if (state.currentStep === 7) state.report = reportText.value
    else ElMessage.info('当前步骤无需执行，可直接下一步。')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '执行失败')
  }
}

async function handleParse() {
  ensureBatchId()
  normalizeJsonInput()
  state.parseResult = state.useMock ? { ...mockParseResult, batch_id: state.batchId } : await parseSource(state)
  ElMessage.success('解析预览已生成')
}

async function handleChunks() {
  if (!state.parseResult) await handleParse()
  state.chunkResult = state.useMock ? { ...mockChunkResult, batch_id: state.batchId } : await previewChunks(state)
  ElMessage.success('切分预览已生成')
}

async function handlePreflight() {
  if (!state.chunkResult) await handleChunks()
  state.preflightResult = state.useMock ? { ...mockPreflightResult, batch_id: state.batchId } : await preflightIngest(state)
  ElMessage.success('预检完成')
}

async function handleRun() {
  if (!state.preflightResult) await handlePreflight()
  if (isPreflightBlocked.value) {
    ElMessage.error('preflight blocked，禁止执行')
    return
  }
  if (state.config.runMode !== 'dry_run') {
    await ElMessageBox.confirm('v1 不开放真实入库。该操作会被后端 blocked。', '真实入库被禁用', { type: 'warning' })
  }
  state.ingestResult = state.useMock ? { ...mockRunResult, batch_id: state.batchId } : await runIngest(state)
  ElMessage.success('dry-run 执行完成')
}

async function handleSearch(query: string, topK: number) {
  if (!query.trim()) {
    ElMessage.warning('请输入检索 query')
    return
  }
  state.searchVerifyResult = state.useMock ? { ...mockSearchResult, query } : await searchVerify(query, topK)
  ElMessage.success('search 验证完成')
}

async function handleAnswer(query: string, topK: number, confirmation: string) {
  if (confirmation !== 'YES_ANSWER') {
    ElMessage.error('请输入 YES_ANSWER')
    return
  }
  state.answerVerifyResult = state.useMock
    ? { ok: true, answer: 'Mock answer', called_answer: true, called_llm: true }
    : await answerVerify(query, topK, confirmation)
  ElMessage.warning('已按手动确认调用 answer 验证')
}

function nextStep() {
  if (state.currentStep < steps.length - 1) {
    state.currentStep += 1
    if (state.currentStep === 7) state.report = reportText.value
  }
}

function buildReport() {
  return [
    '# RAG visual ingest dry-run report',
    '',
    `- batch_id: ${state.batchId || '未生成'}`,
    `- knowledge_action: ${state.config.knowledgeAction}`,
    `- import_mode: ${state.config.importMode}`,
    `- run_mode: ${state.config.runMode}`,
    `- target: ${JSON.stringify(state.config.target)}`,
    `- parse_documents: ${Array.isArray(state.parseResult?.documents) ? state.parseResult.documents.length : 0}`,
    `- chunks: ${state.chunkResult?.chunk_count ?? 0}`,
    `- preflight_status: ${state.preflightResult?.status ?? '未执行'}`,
    `- run_status: ${state.ingestResult?.status ?? '未执行'}`,
    `- answer_called: ${Boolean(state.answerVerifyResult)}`,
    '- embedding_called: false',
    '- vector_db_written: false',
    '- delete_executed: false',
    '- overwrite_executed: false',
    '',
    '## Raw JSON',
    '',
    '```json',
    JSON.stringify(
      {
        uploadResult: state.uploadResult,
        parseResult: state.parseResult,
        chunkResult: state.chunkResult,
        preflightResult: state.preflightResult,
        ingestResult: state.ingestResult,
        searchVerifyResult: state.searchVerifyResult,
        answerVerifyResult: state.answerVerifyResult
      },
      null,
      2
    ),
    '```'
  ].join('\n')
}
</script>

<style scoped>
.step-summary {
  margin-top: 18px;
}
</style>
