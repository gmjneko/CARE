import type {
  AgentFailure,
  InteractionRequestEvent,
  PendingInterrupt,
  RuntimeEvent,
  TokenUsage
} from './runtime-types'

export type ToolCategory = 'read' | 'command' | 'action'

export interface ProjectedToolCall {
  toolCallId: string
  toolName: string
  category: ToolCategory
  arguments: Record<string, unknown>
  output: string
  status: 'running' | 'completed' | 'error'
  durationMs: number | null
}

export interface ToolSummaryCounts {
  actions: number
  reads: number
  commands: number
}

export interface ProjectedSystemNotice {
  id: string
  kind: 'compaction' | 'retry' | 'resumed'
  text: string
}

export interface ProjectedTurn {
  id: string
  runId: string
  nodeId: string | null
  workflowId: string
  userText: string | null
  thinkingText: string
  thinkingStreaming: boolean
  thinkingDurationMs: number | null
  assistantText: string
  toolCalls: ProjectedToolCall[]
  toolCounts: ToolSummaryCounts
  notices: ProjectedSystemNotice[]
  status: 'running' | 'completed' | 'interrupted' | 'failed' | 'aborted'
  startedAt: string
  completedAt: string | null
  durationMs: number | null
  usage: TokenUsage | null
  failure: AgentFailure | null
  runtimeError: { code: string; message: string } | null
}

export interface ProjectedWorkflowNode {
  nodeId: string
  role: string
  adapter: string | null
  status: 'running' | 'completed' | 'interrupted' | 'failed' | 'aborted'
  summary: string
}

export interface ProjectedTimeline {
  turns: ProjectedTurn[]
  nodes: ProjectedWorkflowNode[]
  liveInteractions: Array<{ nodeId: string | null; request: InteractionRequestEvent }>
  workflowInterrupts: PendingInterrupt[]
  latestUsage: TokenUsage | null
  isRunning: boolean
}

const READ_TOOLS = new Set(['read', 'grep', 'find', 'ls', 'glob', 'list_dir', 'search'])
const COMMAND_TOOLS = new Set(['bash', 'shell', 'exec', 'terminal', 'run_command'])

export function classifyTool(toolName: string): ToolCategory {
  const lower = toolName.toLowerCase()
  if (READ_TOOLS.has(lower) || lower.startsWith('read_') || lower.startsWith('search_')) {
    return 'read'
  }
  if (COMMAND_TOOLS.has(lower) || lower.includes('bash') || lower.includes('shell')) {
    return 'command'
  }
  return 'action'
}

export function mergeCanonicalEvents(
  base: readonly RuntimeEvent[],
  incoming: readonly RuntimeEvent[]
): RuntimeEvent[] {
  const seen = new Set<string>()
  const merged: RuntimeEvent[] = []
  for (const ev of base) {
    const key = `${ev.run_id}:${ev.seq}`
    if (!seen.has(key)) {
      seen.add(key)
      merged.push(ev)
    }
  }
  for (const ev of incoming) {
    const key = `${ev.run_id}:${ev.seq}`
    if (!seen.has(key)) {
      seen.add(key)
      merged.push(ev)
    }
  }
  return merged
}

/**
 * Pure deterministic projection turning persisted + live RuntimeEvents into UI Timeline turns,
 * workflow node progress, tool aggregate counters, and pending interactions.
 */
export function projectEventsToTimeline(events: readonly RuntimeEvent[]): ProjectedTimeline {
  const turns: ProjectedTurn[] = []
  const turnByRun = new Map<string, ProjectedTurn>()
  const toolIndex = new Map<string, ProjectedToolCall>()
  const nodesMap = new Map<string, ProjectedWorkflowNode>()
  const liveInteractions: Array<{ nodeId: string | null; request: InteractionRequestEvent }> = []
  let workflowInterrupts: PendingInterrupt[] = []
  let latestUsage: TokenUsage | null = null
  let isRunning = false
  const thinkingStartTs = new Map<string, number>()

  const getOrCreateTurn = (
    ev: RuntimeEvent,
    eventTs: string,
    initialUserText: string | null = null
  ): ProjectedTurn => {
    const existing = turnByRun.get(ev.run_id)
    if (existing) {
      if (initialUserText && !existing.userText) {
        existing.userText = initialUserText
      }
      if (ev.node_id && !existing.nodeId) {
        existing.nodeId = ev.node_id
      }
      return existing
    }
    const created: ProjectedTurn = {
      id: ev.run_id,
      runId: ev.run_id,
      nodeId: ev.node_id ?? null,
      workflowId: ev.workflow_id,
      userText: initialUserText,
      thinkingText: '',
      thinkingStreaming: false,
      thinkingDurationMs: null,
      assistantText: '',
      toolCalls: [],
      toolCounts: { actions: 0, reads: 0, commands: 0 },
      notices: [],
      status: 'running',
      startedAt: eventTs,
      completedAt: null,
      durationMs: null,
      usage: null,
      failure: null,
      runtimeError: null
    }
    turnByRun.set(ev.run_id, created)
    turns.push(created)
    return created
  }

  for (const ev of events) {
    const p = ev.payload
    const eventTs = ev.timestamp ?? new Date().toISOString()
    switch (p.type) {
      case 'workflow.started': {
        isRunning = true
        workflowInterrupts = []
        liveInteractions.length = 0
        getOrCreateTurn(ev, eventTs, p.prompt)
        break
      }

      case 'workflow.resumed': {
        isRunning = true
        workflowInterrupts = []
        liveInteractions.length = 0
        const turn = getOrCreateTurn(ev, eventTs)
        turn.notices.push({
          id: `${ev.run_id}:${ev.seq}`,
          kind: 'resumed',
          text: `已从 ${p.resumed_from} 恢复继续执行`
        })
        break
      }

      case 'node.started': {
        const turn = getOrCreateTurn(ev, eventTs)
        turn.nodeId = p.node_id
        nodesMap.set(p.node_id, {
          nodeId: p.node_id,
          role: p.role,
          adapter: p.adapter ?? null,
          status: 'running',
          summary: ''
        })
        break
      }

      case 'node.completed': {
        const prev = nodesMap.get(p.node_id)
        nodesMap.set(p.node_id, {
          nodeId: p.node_id,
          role: p.role,
          adapter: prev?.adapter ?? null,
          status: p.status,
          summary: p.summary ?? ''
        })
        if (p.usage) {
          latestUsage = p.usage
        }
        break
      }

      case 'turn.started': {
        getOrCreateTurn(ev, eventTs)
        break
      }

      case 'thinking.delta': {
        const turn = getOrCreateTurn(ev, eventTs)
        if (!thinkingStartTs.has(ev.run_id)) {
          thinkingStartTs.set(ev.run_id, Date.parse(eventTs))
        }
        turn.thinkingStreaming = true
        turn.thinkingText += p.delta
        break
      }

      case 'text.delta': {
        const turn = getOrCreateTurn(ev, eventTs)
        if (turn.thinkingStreaming) {
          turn.thinkingStreaming = false
          const started = thinkingStartTs.get(ev.run_id)
          if (started !== undefined) {
            turn.thinkingDurationMs = Math.max(0, Date.parse(eventTs) - started)
          }
        }
        turn.assistantText += p.delta
        break
      }

      case 'message.completed': {
        const turn = getOrCreateTurn(ev, eventTs)
        if (p.role === 'user') {
          if (!turn.userText) {
            turn.userText = p.text
          }
        } else {
          turn.thinkingStreaming = false
          if (p.thinking) {
            turn.thinkingText = p.thinking
            const started = thinkingStartTs.get(ev.run_id)
            if (started !== undefined && turn.thinkingDurationMs === null) {
              turn.thinkingDurationMs = Math.max(0, Date.parse(eventTs) - started)
            }
          }
          if (p.text) {
            turn.assistantText = p.text
          }
          if (p.usage) {
            turn.usage = p.usage
            latestUsage = p.usage
          }
        }
        break
      }

      case 'tool.started': {
        const turn = getOrCreateTurn(ev, eventTs)
        if (turn.thinkingStreaming) {
          turn.thinkingStreaming = false
        }
        const category = classifyTool(p.tool_name)
        const key = `${ev.run_id}:${p.tool_call_id}`
        if (!toolIndex.has(key)) {
          const item: ProjectedToolCall = {
            toolCallId: p.tool_call_id,
            toolName: p.tool_name,
            category,
            arguments: p.arguments ?? {},
            output: '',
            status: 'running',
            durationMs: null
          }
          toolIndex.set(key, item)
          turn.toolCalls.push(item)
          if (category === 'read') turn.toolCounts.reads += 1
          else if (category === 'command') turn.toolCounts.commands += 1
          else turn.toolCounts.actions += 1
        }
        break
      }

      case 'tool.output_delta': {
        const key = `${ev.run_id}:${p.tool_call_id}`
        const item = toolIndex.get(key)
        if (item) {
          item.output += p.chunk
        }
        break
      }

      case 'tool.completed': {
        const key = `${ev.run_id}:${p.tool_call_id}`
        const item = toolIndex.get(key)
        if (item) {
          item.output = p.output
          item.status = p.is_error ? 'error' : 'completed'
          item.durationMs = p.duration_ms ?? null
        }
        break
      }

      case 'compaction': {
        const turn = getOrCreateTurn(ev, eventTs)
        turn.notices.push({
          id: `${ev.run_id}:${ev.seq}`,
          kind: 'compaction',
          text:
            p.phase === 'started'
              ? '正在压缩对话上下文...'
              : `上下文已压缩${p.summary ? `：${p.summary}` : ''}`
        })
        break
      }

      case 'retry': {
        const turn = getOrCreateTurn(ev, eventTs)
        turn.notices.push({
          id: `${ev.run_id}:${ev.seq}`,
          kind: 'retry',
          text:
            p.phase === 'started'
              ? `正在自动重试（第 ${p.attempt ?? 1} 次）...`
              : p.succeeded
                ? `第 ${p.attempt ?? 1} 次重试已恢复`
                : `重试失败${p.error ? `：${p.error}` : ''}`
        })
        break
      }

      case 'usage.updated': {
        const turn = getOrCreateTurn(ev, eventTs)
        turn.usage = p.usage
        latestUsage = p.usage
        break
      }

      case 'interaction.request': {
        if (p.blocking) {
          liveInteractions.push({ nodeId: ev.node_id ?? null, request: p })
        }
        break
      }

      case 'turn.completed': {
        const turn = getOrCreateTurn(ev, eventTs)
        turn.thinkingStreaming = false
        if (p.final_text && !turn.assistantText) {
          turn.assistantText = p.final_text
        }
        if (p.usage) {
          turn.usage = p.usage
          latestUsage = p.usage
        }
        if (p.failure) {
          turn.failure = p.failure
        }
        break
      }

      case 'workflow.interrupted': {
        isRunning = false
        liveInteractions.length = 0
        workflowInterrupts = [...(p.interrupts ?? [])]
        const turn = getOrCreateTurn(ev, eventTs)
        turn.thinkingStreaming = false
        turn.status = 'interrupted'
        turn.completedAt = eventTs
        turn.durationMs = Math.max(0, Date.parse(eventTs) - Date.parse(turn.startedAt))
        // If awaiting_input carries a summary and assistantText wasn't populated, surface it
        for (const intr of workflowInterrupts) {
          const summary =
            typeof intr.payload?.summary === 'string' ? intr.payload.summary : undefined
          if (summary && !turn.assistantText) {
            turn.assistantText = summary
          }
          if (intr.node_id && nodesMap.has(intr.node_id)) {
            const node = nodesMap.get(intr.node_id)!
            node.status = 'interrupted'
          }
        }
        break
      }

      case 'workflow.completed': {
        isRunning = false
        liveInteractions.length = 0
        workflowInterrupts = []
        const turn = getOrCreateTurn(ev, eventTs)
        turn.thinkingStreaming = false
        turn.status = p.status
        turn.completedAt = eventTs
        turn.durationMs = Math.max(0, Date.parse(eventTs) - Date.parse(turn.startedAt))
        if (p.final_output && !turn.assistantText) {
          turn.assistantText = p.final_output
        }
        if (p.failure) {
          turn.failure = p.failure
        }
        break
      }

      case 'runtime.error': {
        isRunning = false
        liveInteractions.length = 0
        const turn = getOrCreateTurn(ev, eventTs)
        turn.thinkingStreaming = false
        turn.status = 'failed'
        turn.completedAt = eventTs
        turn.durationMs = Math.max(0, Date.parse(eventTs) - Date.parse(turn.startedAt))
        turn.runtimeError = { code: p.code, message: p.message }
        break
      }
    }
  }

  return {
    turns,
    nodes: Array.from(nodesMap.values()),
    liveInteractions,
    workflowInterrupts,
    latestUsage,
    isRunning
  }
}
