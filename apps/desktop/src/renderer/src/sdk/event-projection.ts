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

export type ProjectedTurnItem =
  | {
      id: string
      kind: 'thinking'
      text: string
      streaming: boolean
      durationMs: number | null
    }
  | {
      id: string
      kind: 'text'
      text: string
      streaming: boolean
    }
  | {
      id: string
      kind: 'tool_group'
      toolCalls: ProjectedToolCall[]
      counts: ToolSummaryCounts
    }
  | {
      id: string
      kind: 'stage'
      nodeId: string
      role: string
    }
  | {
      id: string
      kind: 'notice'
      noticeKind: 'compaction' | 'retry' | 'resumed'
      text: string
    }

export interface ProjectedTurn {
  id: string
  runId: string
  nodeId: string | null
  workflowId: string
  userText: string | null
  items: ProjectedTurnItem[]
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

interface OpenAssistantStep {
  thinkingItem: Extract<ProjectedTurnItem, { kind: 'thinking' }> | null
  thinkingStartedAtMs: number | null
  textItem: Extract<ProjectedTurnItem, { kind: 'text' }> | null
}

const READ_TOOLS = new Set(['read', 'grep', 'find', 'ls', 'glob', 'list_dir', 'search'])
const COMMAND_TOOLS = new Set(['bash', 'shell', 'exec', 'terminal', 'run_command'])

export function formatWorkflowLabel(workflowId: string): string {
  switch (workflowId) {
    case 'plan_and_execute':
      return '规划执行'
    case 'single_agent':
      return '单 Agent'
    default:
      return workflowId
  }
}

export function formatStageLabel(nodeId: string, role?: string | null): string {
  const key = (role || nodeId).toLowerCase()
  switch (key) {
    case 'planner':
      return '规划阶段'
    case 'executor':
      return '执行阶段'
    case 'reviewer':
      return '评审阶段'
    default:
      return role || nodeId
  }
}

export function formatDurationZh(ms: number | null): string {
  if (ms === null || ms < 0) return ''
  const secs = Math.max(1, Math.round(ms / 1000))
  if (secs < 60) return `${secs}秒`
  const mins = Math.floor(secs / 60)
  const rem = secs % 60
  return rem > 0 ? `${mins}分 ${rem}秒` : `${mins}分`
}

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
 * Pure deterministic projection turning persisted + live RuntimeEvents into chronological
 * turn items (interleaving thinking, model text, tool groups, and notices), workflow node
 * progress, and pending interactions.
 */
export function projectEventsToTimeline(events: readonly RuntimeEvent[]): ProjectedTimeline {
  const turns: ProjectedTurn[] = []
  const turnByRun = new Map<string, ProjectedTurn>()
  const toolIndex = new Map<string, ProjectedToolCall>()
  const openSteps = new Map<string, OpenAssistantStep>()
  const nodesMap = new Map<string, ProjectedWorkflowNode>()
  const liveInteractions: Array<{ nodeId: string | null; request: InteractionRequestEvent }> = []
  let workflowInterrupts: PendingInterrupt[] = []
  let latestUsage: TokenUsage | null = null
  let isRunning = false

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
      items: [],
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

  const getOrCreateStep = (runId: string): OpenAssistantStep => {
    let step = openSteps.get(runId)
    if (!step) {
      step = { thinkingItem: null, thinkingStartedAtMs: null, textItem: null }
      openSteps.set(runId, step)
    }
    return step
  }

  const closeOpenStep = (runId: string, eventTs: string): void => {
    const step = openSteps.get(runId)
    if (!step) return
    if (step.thinkingItem && step.thinkingItem.streaming) {
      step.thinkingItem.streaming = false
      if (step.thinkingItem.durationMs === null && step.thinkingStartedAtMs !== null) {
        step.thinkingItem.durationMs = Math.max(0, Date.parse(eventTs) - step.thinkingStartedAtMs)
      }
    }
    if (step.textItem) {
      step.textItem.streaming = false
    }
    openSteps.delete(runId)
  }

  const ensureFallbackText = (
    turn: ProjectedTurn,
    fallback: string | undefined,
    id: string
  ): void => {
    if (!fallback) return
    const hasText = turn.items.some((item) => item.kind === 'text' && item.text.length > 0)
    if (!hasText) {
      turn.items.push({ id, kind: 'text', text: fallback, streaming: false })
    }
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
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
        turn.items.push({
          id: `${ev.run_id}:notice:${ev.seq}`,
          kind: 'notice',
          noticeKind: 'resumed',
          text: `已从 ${p.resumed_from} 恢复继续执行`
        })
        break
      }

      case 'node.started': {
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
        turn.nodeId = p.node_id
        if (ev.workflow_id !== 'single_agent') {
          turn.items.push({
            id: `${ev.run_id}:stage:${ev.seq}`,
            kind: 'stage',
            nodeId: p.node_id,
            role: p.role
          })
        }
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
        closeOpenStep(ev.run_id, eventTs)
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
        const step = getOrCreateStep(ev.run_id)
        if (!step.thinkingItem) {
          const item: Extract<ProjectedTurnItem, { kind: 'thinking' }> = {
            id: `${ev.run_id}:think:${ev.seq}`,
            kind: 'thinking',
            text: p.delta,
            streaming: true,
            durationMs: null
          }
          step.thinkingItem = item
          step.thinkingStartedAtMs = Date.parse(eventTs)
          turn.items.push(item)
        } else {
          step.thinkingItem.text += p.delta
        }
        break
      }

      case 'text.delta': {
        const turn = getOrCreateTurn(ev, eventTs)
        const step = getOrCreateStep(ev.run_id)
        if (step.thinkingItem && step.thinkingItem.streaming) {
          step.thinkingItem.streaming = false
          if (step.thinkingStartedAtMs !== null) {
            step.thinkingItem.durationMs = Math.max(
              0,
              Date.parse(eventTs) - step.thinkingStartedAtMs
            )
          }
        }
        if (!step.textItem) {
          const item: Extract<ProjectedTurnItem, { kind: 'text' }> = {
            id: `${ev.run_id}:text:${ev.seq}`,
            kind: 'text',
            text: p.delta,
            streaming: true
          }
          step.textItem = item
          turn.items.push(item)
        } else {
          step.textItem.text += p.delta
        }
        break
      }

      case 'message.completed': {
        const turn = getOrCreateTurn(ev, eventTs)
        if (p.role === 'user') {
          if (!turn.userText) {
            turn.userText = p.text
          }
        } else {
          const step = openSteps.get(ev.run_id)
          if (step?.thinkingItem) {
            step.thinkingItem.streaming = false
            if (step.thinkingItem.durationMs === null && step.thinkingStartedAtMs !== null) {
              step.thinkingItem.durationMs = Math.max(
                0,
                Date.parse(eventTs) - step.thinkingStartedAtMs
              )
            }
            if (p.thinking) {
              step.thinkingItem.text = p.thinking
            }
          } else if (p.thinking) {
            turn.items.push({
              id: `${ev.run_id}:think:${ev.seq}`,
              kind: 'thinking',
              text: p.thinking,
              streaming: false,
              durationMs: null
            })
          }

          if (step?.textItem) {
            step.textItem.streaming = false
            if (p.text) {
              step.textItem.text = p.text
            }
          } else if (p.text) {
            turn.items.push({
              id: `${ev.run_id}:text:${ev.seq}`,
              kind: 'text',
              text: p.text,
              streaming: false
            })
          }

          openSteps.delete(ev.run_id)

          if (p.usage) {
            turn.usage = p.usage
            latestUsage = p.usage
          }
        }
        break
      }

      case 'tool.started': {
        const turn = getOrCreateTurn(ev, eventTs)
        closeOpenStep(ev.run_id, eventTs)
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

          const last = turn.items[turn.items.length - 1]
          if (last && last.kind === 'tool_group') {
            last.toolCalls.push(item)
            if (category === 'read') last.counts.reads += 1
            else if (category === 'command') last.counts.commands += 1
            else last.counts.actions += 1
          } else {
            turn.items.push({
              id: `${ev.run_id}:tools:${ev.seq}`,
              kind: 'tool_group',
              toolCalls: [item],
              counts: {
                actions: category === 'action' ? 1 : 0,
                reads: category === 'read' ? 1 : 0,
                commands: category === 'command' ? 1 : 0
              }
            })
          }
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
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
        turn.items.push({
          id: `${ev.run_id}:notice:${ev.seq}`,
          kind: 'notice',
          noticeKind: 'compaction',
          text:
            p.phase === 'started'
              ? '正在压缩对话上下文...'
              : `上下文已压缩${p.summary ? `：${p.summary}` : ''}`
        })
        break
      }

      case 'retry': {
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
        turn.items.push({
          id: `${ev.run_id}:notice:${ev.seq}`,
          kind: 'notice',
          noticeKind: 'retry',
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
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
        ensureFallbackText(turn, p.final_text, `${ev.run_id}:turn_final:${ev.seq}`)
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
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
        turn.status = 'interrupted'
        turn.completedAt = eventTs
        turn.durationMs = Math.max(0, Date.parse(eventTs) - Date.parse(turn.startedAt))
        for (const intr of workflowInterrupts) {
          const summary =
            typeof intr.payload?.summary === 'string' ? intr.payload.summary : undefined
          ensureFallbackText(turn, summary, `${ev.run_id}:intr_summary:${ev.seq}`)
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
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
        turn.status = p.status
        turn.completedAt = eventTs
        turn.durationMs = Math.max(0, Date.parse(eventTs) - Date.parse(turn.startedAt))
        ensureFallbackText(turn, p.final_output, `${ev.run_id}:wf_final:${ev.seq}`)
        if (p.failure) {
          turn.failure = p.failure
        }
        break
      }

      case 'runtime.error': {
        isRunning = false
        liveInteractions.length = 0
        closeOpenStep(ev.run_id, eventTs)
        const turn = getOrCreateTurn(ev, eventTs)
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
