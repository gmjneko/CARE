import type {
  AgentFailure,
  InteractionRequestEvent,
  PendingInterrupt,
  RuntimeEvent,
  TokenUsage
} from './generated/canonical-event'

export type { AgentFailure, InteractionRequestEvent, PendingInterrupt, RuntimeEvent, TokenUsage }

export type RuntimeMode = 'local' | 'cloud'
export type ThinkingLevel = 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export type ApiProtocol =
  'openai-completions' | 'openai-responses' | 'anthropic-messages' | 'google-generative-ai'
export type RunStatus = 'running' | 'completed' | 'interrupted' | 'failed' | 'aborted'
export type ThreadStatus =
  'completed' | 'interrupted' | 'aborted' | 'failed' | 'crashed' | 'running'
export type InteractionMode = 'wait' | 'cancel'

export interface HealthResponse {
  status: 'ok'
  mode: RuntimeMode
  version: string
}

export interface WorkflowInfo {
  name: string
  description: string
}

export interface AgentCapabilities {
  streaming: boolean
  steering: boolean
  follow_up: boolean
  abort: boolean
  session_resume: boolean
  session_fork: boolean
  interactions: boolean
}

export interface AdapterInfo {
  kind: string
  capabilities: AgentCapabilities
}

export interface RunSnapshot {
  run_id: string
  thread_id: string
  workflow_id: string
  status: RunStatus
  last_seq: number
  error: string | null
}

export interface ThreadSummary {
  thread_id: string
  workflow_id: string
  workspace_dir: string
  title: string
  status: ThreadStatus
  active_run_id: string | null
  last_run_id: string | null
  created_at: string
  updated_at: string
}

export interface NodeOutput {
  node_id: string
  role: string
  status: 'completed' | 'waiting' | 'interrupted' | 'failed' | 'aborted'
  summary: string
  adapter?: string | null
  session_ref?: string | null
  usage?: TokenUsage | null
  failure?: AgentFailure | null
  pending_interaction?: InteractionRequestEvent | null
}

export interface InflightNodeRecord {
  thread_id: string
  node_id: string
  role: string
  task_id: string
  session_ref: string | null
  pending_interaction: InteractionRequestEvent | null
  updated_at: string
}

export interface ThreadState {
  thread_id: string
  status: ThreadStatus
  resumable: boolean
  active_run_id: string | null
  node_outputs: Record<string, NodeOutput>
  node_sessions: Record<string, string>
  next_nodes: string[]
  interrupts: PendingInterrupt[]
  inflight_nodes: InflightNodeRecord[]
}

export interface InteractionResponse {
  request_id: string
  decision: 'approve' | 'deny' | 'cancel'
  feedback?: string | null
  value?: string | null
}

export interface StartRunRequest {
  workflow_id: string
  prompt: string
  workspace_dir: string
  run_id?: string | null
  thread_id?: string | null
  adapter?: string
  profile?: string | null
  thinking_level?: ThinkingLevel | null
  persist_session?: boolean
  interaction_mode?: InteractionMode
}

export interface ResumeThreadRequest {
  run_id?: string | null
  node_id?: string | null
  resume_value?: unknown
  interaction_response?: InteractionResponse | null
  prompt?: string | null
  complete?: boolean
  adapter?: string | null
  profile?: string | null
  thinking_level?: ThinkingLevel | null
  persist_session?: boolean | null
  interaction_mode?: InteractionMode
}

export interface ModelProfileView {
  model: string
  provider: string | null
  base_url: string | null
  api_protocol: ApiProtocol
  thinking_level: ThinkingLevel | null
  has_api_key: boolean
}

export interface LocalSettingsResponse {
  active_profile: string | null
  profiles: Record<string, ModelProfileView>
  adapter_bins: Record<string, string>
}

export interface ModelProfileInput {
  model: string
  provider?: string | null
  base_url?: string | null
  api_protocol?: ApiProtocol
  /** Omit to preserve existing stored key; pass null to clear; pass string to set/rotate. */
  api_key?: string | null
  thinking_level?: ThinkingLevel | null
}

export interface LocalSettingsUpdate {
  active_profile: string | null
  profiles: Record<string, ModelProfileInput>
  adapter_bins: Record<string, string>
}
