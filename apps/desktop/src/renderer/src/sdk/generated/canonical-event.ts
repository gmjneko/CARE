/* eslint-disable */
/**
 * AUTO-GENERATED from contracts/canonical-event.schema.json.
 * Do not edit manually — run `pnpm contracts:sync` to regenerate.
 */

export type IsEphemeral = boolean
export type NodeId = string | null
export type Payload =
  | TurnStartedEvent
  | TextDeltaEvent
  | ThinkingDeltaEvent
  | MessageCompletedEvent
  | ToolStartedEvent
  | ToolOutputDeltaEvent
  | ToolCompletedEvent
  | CompactionEvent
  | RetryEvent
  | InteractionRequestEvent
  | UsageUpdatedEvent
  | TurnCompletedEvent
  | WorkflowStartedEvent
  | WorkflowResumedEvent
  | NodeStartedEvent
  | NodeCompletedEvent
  | WorkflowInterruptedEvent
  | WorkflowCompletedEvent
  | RuntimeErrorEvent
export type SessionId = string | null
export type TurnId = string
export type Type = 'turn.started'
export type ContentIndex = number
export type Delta = string
export type Type1 = 'text.delta'
export type ContentIndex1 = number
export type Delta1 = string
export type Type2 = 'thinking.delta'
export type Model = string | null
export type Role = 'user' | 'assistant'
export type Text = string
export type Thinking = string | null
export type Type3 = 'message.completed'
export type CachedInputTokens = number
export type InputTokens = number
export type OutputTokens = number
export type TotalTokens = number
export type ParentToolCallId = string | null
export type ToolCallId = string
export type ToolName = string
export type Type4 = 'tool.started'
export type Chunk = string
export type ToolCallId1 = string
export type ToolName1 = string
export type Type5 = 'tool.output_delta'
export type DurationMs = number | null
export type IsError = boolean
export type Output = string
export type ToolCallId2 = string
export type ToolName2 = string
export type Type6 = 'tool.completed'
export type Phase = 'started' | 'completed'
export type Reason = string | null
export type Summary = string | null
export type Type7 = 'compaction'
export type Attempt = number
export type Error = string | null
export type MaxAttempts = number | null
export type Phase1 = 'started' | 'completed'
export type Succeeded = boolean | null
export type Type8 = 'retry'
export type Arguments1 = {
  [k: string]: unknown
} | null
export type Blocking = boolean
export type InputMode = 'select' | 'text' | null
export type Kind = 'approval' | 'user_input' | 'notify'
export type Message = string | null
export type Options = string[] | null
export type RequestId = string
export type Title = string | null
export type ToolCallId3 = string | null
export type ToolName3 = string | null
export type Type9 = 'interaction.request'
export type Type10 = 'usage.updated'
export type Code = string | null
export type FailureClass =
  | 'provider_error'
  | 'transport_error'
  | 'permission_error'
  | 'protocol_error'
  | 'usage_limit'
  | 'unknown'
export type Message1 = string
export type Retryable = boolean
export type FinalText = string
export type SessionReusable = boolean
export type Status = 'completed' | 'aborted' | 'failed'
export type TurnId1 = string
export type Type11 = 'turn.completed'
export type Prompt = string
export type Type12 = 'workflow.started'
export type ResumedFrom =
  'workflow_interrupt' | 'agent_interaction' | 'awaiting_input' | 'failure' | 'crash'
export type Type13 = 'workflow.resumed'
export type Adapter = string | null
export type NodeId1 = string
export type Role1 = string
export type Type14 = 'node.started'
export type NodeId2 = string
export type Role2 = string
export type Status1 = 'completed' | 'interrupted' | 'failed' | 'aborted'
export type Summary1 = string
export type Type15 = 'node.completed'
export type InterruptId = string | null
export type Kind1 =
  | 'workflow_interrupt'
  | 'agent_interaction'
  | 'awaiting_input'
  | 'suspended'
  | 'failed'
  | 'aborted'
  | 'crashed'
export type NodeId3 = string
export type Interrupts = PendingInterrupt[]
export type Type16 = 'workflow.interrupted'
export type FinalOutput = string
export type Status2 = 'completed' | 'failed' | 'aborted'
export type Type17 = 'workflow.completed'
export type Code1 = 'adapter_error' | 'internal_error'
export type Message2 = string
export type Type18 = 'runtime.error'
export type RunId = string
export type Seq = number
export type ThreadId = string
export type Timestamp = string
export type WorkflowId = string

/**
 * Sequenced canonical event envelope emitted by WorkflowRunner.
 */
export interface RuntimeEvent {
  is_ephemeral: IsEphemeral
  node_id?: NodeId
  payload: Payload
  run_id: RunId
  seq: Seq
  thread_id: ThreadId
  timestamp?: Timestamp
  workflow_id: WorkflowId
}
export interface TurnStartedEvent {
  session_id?: SessionId
  turn_id: TurnId
  type?: Type
}
export interface TextDeltaEvent {
  content_index?: ContentIndex
  delta: Delta
  type?: Type1
}
export interface ThinkingDeltaEvent {
  content_index?: ContentIndex1
  delta: Delta1
  type?: Type2
}
export interface MessageCompletedEvent {
  model?: Model
  role: Role
  text: Text
  thinking?: Thinking
  type?: Type3
  usage?: TokenUsage | null
}
/**
 * Normalized token accounting reported by an agent turn or message.
 */
export interface TokenUsage {
  cached_input_tokens?: CachedInputTokens
  input_tokens?: InputTokens
  output_tokens?: OutputTokens
  total_tokens?: TotalTokens
}
export interface ToolStartedEvent {
  arguments?: Arguments
  parent_tool_call_id?: ParentToolCallId
  tool_call_id: ToolCallId
  tool_name: ToolName
  type?: Type4
}
export interface Arguments {
  [k: string]: unknown
}
export interface ToolOutputDeltaEvent {
  chunk: Chunk
  tool_call_id: ToolCallId1
  tool_name: ToolName1
  type?: Type5
}
export interface ToolCompletedEvent {
  duration_ms?: DurationMs
  is_error?: IsError
  output: Output
  tool_call_id: ToolCallId2
  tool_name: ToolName2
  type?: Type6
}
export interface CompactionEvent {
  phase: Phase
  reason?: Reason
  summary?: Summary
  type?: Type7
}
export interface RetryEvent {
  attempt?: Attempt
  error?: Error
  max_attempts?: MaxAttempts
  phase: Phase1
  succeeded?: Succeeded
  type?: Type8
}
export interface InteractionRequestEvent {
  arguments?: Arguments1
  blocking?: Blocking
  input_mode?: InputMode
  kind: Kind
  message?: Message
  options?: Options
  request_id: RequestId
  title?: Title
  tool_call_id?: ToolCallId3
  tool_name?: ToolName3
  type?: Type9
}
export interface UsageUpdatedEvent {
  type?: Type10
  usage: TokenUsage
}
export interface TurnCompletedEvent {
  failure?: AgentFailure | null
  final_text?: FinalText
  session_reusable?: SessionReusable
  status: Status
  turn_id: TurnId1
  type?: Type11
  usage?: TokenUsage | null
}
/**
 * Structured, redacted failure metadata for routing and diagnostics.
 */
export interface AgentFailure {
  code?: Code
  failure_class: FailureClass
  message: Message1
  retryable?: Retryable
}
export interface WorkflowStartedEvent {
  prompt: Prompt
  type?: Type12
}
/**
 * 工作流从历史断点恢复执行时发射的起始事件。
 */
export interface WorkflowResumedEvent {
  resumed_from: ResumedFrom
  type?: Type13
}
export interface NodeStartedEvent {
  adapter?: Adapter
  node_id: NodeId1
  role: Role1
  type?: Type14
}
export interface NodeCompletedEvent {
  failure?: AgentFailure | null
  node_id: NodeId2
  role: Role2
  status: Status1
  summary?: Summary1
  type?: Type15
  usage?: TokenUsage | null
}
/**
 * 工作流因节点间 interrupt() 或节点内待回复交互/挂起而暂停时发射的终态事件。
 */
export interface WorkflowInterruptedEvent {
  interrupts?: Interrupts
  type?: Type16
}
/**
 * 一个等待外部决策或恢复的挂起分支（Agent 节点内暂停、Workflow interrupt 或崩溃任务）。
 */
export interface PendingInterrupt {
  failure?: AgentFailure | null
  interaction?: InteractionRequestEvent | null
  interrupt_id?: InterruptId
  kind: Kind1
  node_id: NodeId3
  payload?: Payload1
}
export interface Payload1 {
  [k: string]: unknown
}
export interface WorkflowCompletedEvent {
  failure?: AgentFailure | null
  final_output?: FinalOutput
  status: Status2
  type?: Type17
}
/**
 * Terminal runtime/transport error frame when a run stream terminates with an exception.
 */
export interface RuntimeErrorEvent {
  code: Code1
  message: Message2
  type?: Type18
}
