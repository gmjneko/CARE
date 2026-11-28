import { describe, expect, it } from 'vitest'
import { ContributionRegistry } from '@renderer/shell/registries'
import { classifyTool, mergeCanonicalEvents, projectEventsToTimeline } from './event-projection'
import type { RuntimeEvent } from './runtime-types'

function makeEvent(
  seq: number,
  payload: RuntimeEvent['payload'],
  overrides?: Partial<RuntimeEvent>
): RuntimeEvent {
  return {
    seq,
    timestamp: `2026-11-28T07:00:0${seq}.000Z`,
    run_id: overrides?.run_id ?? 'run-1',
    thread_id: overrides?.thread_id ?? 'thread-1',
    workflow_id: overrides?.workflow_id ?? 'plan_and_execute',
    node_id: overrides?.node_id ?? null,
    is_ephemeral:
      payload.type === 'text.delta' ||
      payload.type === 'thinking.delta' ||
      payload.type === 'tool.output_delta',
    payload
  }
}

describe('event-projection & ContributionRegistry', () => {
  it('deduplicates merged history + live SSE events by (run_id, seq)', () => {
    const e1 = makeEvent(1, { type: 'workflow.started', prompt: 'Hello' })
    const e2 = makeEvent(2, { type: 'text.delta', delta: 'Hi', content_index: 0 })
    const e3 = makeEvent(3, {
      type: 'workflow.completed',
      status: 'completed',
      final_output: 'Hi',
      failure: null
    })
    const merged = mergeCanonicalEvents([e1, e2], [e2, e3])
    expect(merged.map((e) => e.seq)).toEqual([1, 2, 3])
  })

  it('projects thinking, categorized tools, authoritative message, and workflow interrupts', () => {
    const events: RuntimeEvent[] = [
      makeEvent(1, { type: 'workflow.started', prompt: 'Plan feature X' }),
      makeEvent(2, { type: 'node.started', node_id: 'planner', role: 'planner', adapter: 'pi' }),
      makeEvent(3, { type: 'thinking.delta', delta: 'Analyzing...', content_index: 0 }),
      makeEvent(4, {
        type: 'tool.started',
        tool_call_id: 'tc-1',
        tool_name: 'read',
        arguments: { path: 'README.md' }
      }),
      makeEvent(5, {
        type: 'tool.completed',
        tool_call_id: 'tc-1',
        tool_name: 'read',
        output: '# CARE',
        is_error: false,
        duration_ms: 42
      }),
      makeEvent(6, {
        type: 'tool.started',
        tool_call_id: 'tc-2',
        tool_name: 'bash',
        arguments: { command: 'git status' }
      }),
      makeEvent(7, {
        type: 'tool.output_delta',
        tool_call_id: 'tc-2',
        tool_name: 'bash',
        chunk: 'On branch main'
      }),
      makeEvent(8, {
        type: 'tool.completed',
        tool_call_id: 'tc-2',
        tool_name: 'bash',
        output: 'On branch main\n',
        is_error: false,
        duration_ms: 120
      }),
      makeEvent(9, {
        type: 'message.completed',
        role: 'assistant',
        text: 'Draft plan ready.',
        thinking: 'Analyzing...',
        usage: { input_tokens: 100, output_tokens: 40, cached_input_tokens: 20, total_tokens: 140 }
      }),
      makeEvent(10, {
        type: 'workflow.interrupted',
        interrupts: [
          {
            node_id: 'planner',
            kind: 'awaiting_input',
            interrupt_id: 'intr-1',
            interaction: null,
            failure: null,
            payload: { summary: 'Draft plan ready.' }
          }
        ]
      })
    ]

    const timeline = projectEventsToTimeline(events)
    expect(timeline.isRunning).toBe(false)
    expect(timeline.turns).toHaveLength(1)
    expect(timeline.turns[0].userText).toBe('Plan feature X')
    expect(timeline.turns[0].assistantText).toBe('Draft plan ready.')
    expect(timeline.turns[0].toolCounts).toEqual({ actions: 0, reads: 1, commands: 1 })
    expect(timeline.turns[0].status).toBe('interrupted')
    expect(timeline.nodes).toEqual([
      {
        nodeId: 'planner',
        role: 'planner',
        adapter: 'pi',
        status: 'interrupted',
        summary: ''
      }
    ])
    expect(timeline.workflowInterrupts).toHaveLength(1)
    expect(timeline.latestUsage?.total_tokens).toBe(140)
    expect(classifyTool('edit')).toBe('action')
  })

  it('orders contributions deterministically and fails fast on duplicate ids', () => {
    const reg = new ContributionRegistry<{ id: string; order: number }>()
    reg.register({ id: 'b', order: 20 })
    reg.register({ id: 'a', order: 10 })
    expect(reg.list().map((i) => i.id)).toEqual(['a', 'b'])
    expect(() => reg.register({ id: 'a', order: 30 })).toThrow(/Duplicate contribution id/)
  })
})
