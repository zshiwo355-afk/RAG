export type RunMode = 'dry_run' | 'real_run'
export type ImportMode = 'supplement' | 'append' | 'partial' | 'full' | 'overwrite'
export type KnowledgeAction = 'supplement_existing' | 'create_new_knowledge'
export type InputType = 'text' | 'json' | 'file'

export interface TargetConfig {
  index?: string
  collection?: string
  namespace?: string
}

export interface SourceInput {
  inputType: InputType
  rawText: string
  rawJson?: unknown
  filePath?: string
  sourceName: string
  sourceId: string
  metadata: Record<string, unknown>
}

export interface IngestConfig {
  knowledgeAction: KnowledgeAction
  importMode: ImportMode
  runMode: RunMode
  target: TargetConfig
  chunkSize: number
  overlap: number
  overwrite: boolean
  answerVerify: boolean
  newKnowledgeName: string
  newKnowledgeDescription: string
  tags: string[]
  version: string
}

export interface VisualIngestState {
  currentStep: number
  batchId: string
  useMock: boolean
  sourceInput: SourceInput
  config: IngestConfig
  uploadResult?: Record<string, unknown>
  parseResult?: Record<string, unknown>
  chunkResult?: Record<string, unknown>
  preflightResult?: Record<string, unknown>
  ingestResult?: Record<string, unknown>
  searchVerifyResult?: Record<string, unknown>
  answerVerifyResult?: Record<string, unknown>
  report: string
  errors: string[]
  warnings: string[]
}

export interface ApiPayload {
  batch_id?: string
  source_input?: Record<string, unknown>
  config?: Record<string, unknown>
  parse_result?: unknown
  chunk_result?: unknown
  preflight_result?: unknown
}
