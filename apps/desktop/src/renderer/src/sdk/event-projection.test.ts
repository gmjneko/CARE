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
    timestamp: `2026-11-28T07:00:${String(seq).padStart(2, '0')}.000Z`,
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

  it('preserves chronological interleaving of thinking, narration, tool groups, and final reply (live & replay)', () => {
    const fullEvents: RuntimeEvent[] = [
      makeEvent(1, { type: 'workflow.started', prompt: 'Check git status' }),
      makeEvent(2, { type: 'thinking.delta', delta: 'Inspecting repo...', content_index: 0 }),
      makeEvent(3, { type: 'text.delta', delta: '我先看一下 Git 状态。', content_index: 0 }),
      makeEvent(4, {
        type: 'message.completed',
        role: 'assistant',
        text: '我先看一下 Git 状态。',
        thinking: 'Inspecting repo...',
        usage: null
      }),
      makeEvent(5, {
        type: 'tool.started',
        tool_call_id: 'tc-1',
        tool_name: 'bash',
        arguments: { command: 'git status' }
      }),
      makeEvent(6, {
        type: 'tool.completed',
        tool_call_id: 'tc-1',
        tool_name: 'bash',
        output: 'On branch main',
        is_error: false,
        duration_ms: 80
      }),
      makeEvent(7, { type: 'text.delta', delta: '接下来核对 build/ 规则。', content_index: 0 }),
      makeEvent(8, {
        type: 'message.completed',
        role: 'assistant',
        text: '接下来核对 build/ 规则。',
        thinking: null,
        usage: null
      }),
      makeEvent(9, {
        type: 'tool.started',
        tool_call_id: 'tc-2',
        tool_name: 'bash',
        arguments: { command: 'python3 check1.py' }
      }),
      makeEvent(10, {
        type: 'tool.started',
        tool_call_id: 'tc-3',
        tool_name: 'bash',
        arguments: { command: 'python3 check2.py' }
      }),
      makeEvent(11, {
        type: 'tool.completed',
        tool_call_id: 'tc-2',
        tool_name: 'bash',
        output: 'ok',
        is_error: false,
        duration_ms: 50
      }),
      makeEvent(12, {
        type: 'tool.completed',
        tool_call_id: 'tc-3',
        tool_name: 'bash',
        output: 'ok',
        is_error: false,
        duration_ms: 60
      }),
      makeEvent(13, {
        type: 'text.delta',
        delta: '建议把脚手架作为独立 commit。',
        content_index: 0
      }),
      makeEvent(14, {
        type: 'message.completed',
        role: 'assistant',
        text: '建议把脚手架作为独立 commit。',
        thinking: null,
        usage: { input_tokens: 100, output_tokens: 40, cached_input_tokens: 20, total_tokens: 140 }
      }),
      makeEvent(15, {
        type: 'workflow.completed',
        status: 'completed',
        final_output: '建议把脚手架作为独立 commit。',
        failure: null
      })
    ]

    const liveTimeline = projectEventsToTimeline(fullEvents)
    const replayTimeline = projectEventsToTimeline(fullEvents.filter((e) => !e.is_ephemeral))

    for (const timeline of [liveTimeline, replayTimeline]) {
      expect(timeline.turns).toHaveLength(1)
      const items = timeline.turns[0].items
      expect(items.map((i) => i.kind)).toEqual([
        'thinking',
        'text',
        'tool_group',
        'text',
        'tool_group',
        'text'
      ])
      expect(items[0].kind === 'thinking' && items[0].text).toBe('Inspecting repo...')
      expect(items[1].kind === 'text' && items[1].text).toBe('我先看一下 Git 状态。')
      expect(items[2].kind === 'tool_group' && items[2].toolCalls.map((c) => c.toolCallId)).toEqual(
        ['tc-1']
      )
      expect(items[3].kind === 'text' && items[3].text).toBe('接下来核对 build/ 规则。')
      expect(items[4].kind === 'tool_group' && items[4].toolCalls.map((c) => c.toolCallId)).toEqual(
        ['tc-2', 'tc-3']
      )
      expect(items[4].kind === 'tool_group' && items[4].counts).toEqual({
        actions: 0,
        reads: 0,
        commands: 2
      })
      expect(items[5].kind === 'text' && items[5].text).toBe('建议把脚手架作为独立 commit。')
    }
  })

  it('projects categorized tools, authoritative message, and workflow interrupts', () => {
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
        thinking: null,
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
    expect(timeline.turns[0].items.map((i) => i.kind)).toEqual(['thinking', 'tool_group', 'text'])
    const toolGroup = timeline.turns[0].items[1]
    expect(toolGroup.kind === 'tool_group' && toolGroup.counts).toEqual({
      actions: 0,
      reads: 1,
      commands: 1
    })
    const finalText = timeline.turns[0].items[2]
    expect(finalText.kind === 'text' && finalText.text).toBe('Draft plan ready.')
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
