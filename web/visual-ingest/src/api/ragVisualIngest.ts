import type { ApiPayload, VisualIngestState } from '../types/ragVisualIngest'

const API_PREFIX = '/api/rag/visual-ingest'

function toBackendState(state: VisualIngestState): ApiPayload {
  return {
    batch_id: state.batchId || undefined,
    source_input: {
      input_type: state.sourceInput.inputType,
      raw_text: state.sourceInput.rawText,
      raw_json: state.sourceInput.rawJson,
      file_path: state.sourceInput.filePath,
      source_name: state.sourceInput.sourceName,
      source_id: state.sourceInput.sourceId,
      metadata: state.sourceInput.metadata
    },
    config: {
      knowledge_action: state.config.knowledgeAction,
      import_mode: state.config.importMode,
      run_mode: state.config.runMode,
      target: state.config.target,
      chunk_size: state.config.chunkSize,
      overlap: state.config.overlap,
      overwrite: state.config.overwrite,
      answer_verify: state.config.answerVerify,
      new_knowledge_name: state.config.newKnowledgeName,
      new_knowledge_description: state.config.newKnowledgeDescription,
      tags: state.config.tags,
      version: state.config.version
    },
    parse_result: state.parseResult,
    chunk_result: state.chunkResult,
    preflight_result: state.preflightResult
  }
}

async function postJson<T>(url: string, payload: unknown): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

export async function uploadFile(file: File, batchId?: string): Promise<Record<string, unknown>> {
  const content = await file.arrayBuffer()
  const bytes = new Uint8Array(content)
  let binary = ''
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte)
  })
  return postJson(`${API_PREFIX}/upload`, {
    filename: file.name,
    content_base64: btoa(binary),
    batch_id: batchId || undefined
  })
}

export function parseSource(state: VisualIngestState): Promise<Record<string, unknown>> {
  return postJson(`${API_PREFIX}/parse`, toBackendState(state))
}

export function previewChunks(state: VisualIngestState): Promise<Record<string, unknown>> {
  return postJson(`${API_PREFIX}/chunks/preview`, toBackendState(state))
}

export function preflightIngest(state: VisualIngestState): Promise<Record<string, unknown>> {
  return postJson(`${API_PREFIX}/preflight`, toBackendState(state))
}

export function runIngest(state: VisualIngestState): Promise<Record<string, unknown>> {
  return postJson(`${API_PREFIX}/run`, toBackendState(state))
}

export function searchVerify(query: string, topK: number): Promise<Record<string, unknown>> {
  return postJson(`${API_PREFIX}/search-verify`, { query, top_k: topK })
}

export function answerVerify(query: string, topK: number, confirmation: string): Promise<Record<string, unknown>> {
  return postJson(`${API_PREFIX}/answer-verify`, {
    query,
    top_k: topK,
    answer_verify: true,
    confirmation
  })
}
