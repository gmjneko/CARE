import { memo, useEffect, useMemo, useRef, useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import { AlertCircle, Brain, CheckCircle2, ChevronRight, CircleDot, Loader2 } from 'lucide-react'
import {
  projectEventsToTimeline,
  type ProjectedTurn,
  type ProjectedTurnItem,
  type ProjectedWorkflowNode
} from '@renderer/sdk/event-projection'
import { useSessionStore } from '@renderer/stores/session-store'
import { cn } from '@renderer/ui/cn'
import { MarkdownRenderer } from '@renderer/ui/markdown/MarkdownRenderer'
import { ComposerCard } from './ComposerCard'
import { InteractionBanner } from './InteractionBanner'
import { ToolGroupSummary } from './ToolGroupSummary'

function formatDuration(ms: number | null): string {
  if (ms === null || ms < 0) return ''
  const secs = Math.max(1, Math.round(ms / 1000))
  if (secs < 60) return `${secs}秒`
  const mins = Math.floor(secs / 60)
  const rem = secs % 60
  return rem > 0 ? `${mins}分 ${rem}秒` : `${mins}分`
}

function LiveElapsed({ startedAt }: { startedAt: string }): React.JSX.Element {
  const [elapsed, setElapsed] = useState(() => Math.max(0, Date.now() - Date.parse(startedAt)))

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsed(Math.max(0, Date.now() - Date.parse(startedAt)))
    }, 1000)
    return () => clearInterval(timer)
  }, [startedAt])

  return <span>{formatDuration(elapsed)}</span>
}

function ThinkingBlock({
  item
}: {
  item: Extract<ProjectedTurnItem, { kind: 'thinking' }>
}): React.JSX.Element {
  const [open, setOpen] = useState(false)

  return (
    <Collapsible.Root open={open} onOpenChange={setOpen}>
      <Collapsible.Trigger className="inline-flex items-center gap-1.5 rounded-lg py-0.5 text-ui text-[var(--text-muted)] transition-colors hover:text-[var(--text-secondary)]">
        <Brain className="h-3.5 w-3.5" strokeWidth={1.6} />
        <span>
          {item.streaming
            ? '正在思考...'
            : item.durationMs !== null
              ? `已思考 ${formatDuration(item.durationMs)}`
              : '已思考'}
        </span>
        <ChevronRight className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-90')} />
      </Collapsible.Trigger>
      <Collapsible.Content className="mt-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/50 px-3.5 py-2.5 text-ui text-[var(--text-secondary)] whitespace-pre-wrap">
        {item.text}
      </Collapsible.Content>
    </Collapsible.Root>
  )
}

function TurnItemList({ items }: { items: ProjectedTurnItem[] }): React.JSX.Element {
  return (
    <div className="space-y-3">
      {items.map((item) => {
        switch (item.kind) {
          case 'thinking':
            return <ThinkingBlock key={item.id} item={item} />
          case 'text':
            return <MarkdownRenderer key={item.id} content={item.text} />
          case 'tool_group':
            return (
              <ToolGroupSummary key={item.id} toolCalls={item.toolCalls} counts={item.counts} />
            )
          case 'notice':
            return (
              <div key={item.id} className="flex flex-wrap gap-1.5">
                <span className="rounded-full bg-[var(--bg-subtle)] px-2.5 py-0.5 text-caption text-[var(--text-muted)]">
                  {item.text}
                </span>
              </div>
            )
        }
      })}
    </div>
  )
}

const TurnItem = memo(function TurnItem({ turn }: { turn: ProjectedTurn }): React.JSX.Element {
  const [processOpen, setProcessOpen] = useState(false)
  const isRunning = turn.status === 'running'

  const lastItem = turn.items[turn.items.length - 1]
  const hasFinalReply = !isRunning && lastItem?.kind === 'text'
  const processItems = hasFinalReply ? turn.items.slice(0, -1) : turn.items
  const finalItem = hasFinalReply ? lastItem : null

  const durationText = formatDuration(turn.durationMs)
  const collapsedLabel = durationText ? `用时 ${durationText}` : '执行过程'

  return (
    <div className="space-y-4">
      {/* Right-aligned soft user message bubble */}
      {turn.userText && (
        <div className="flex justify-end">
          <div className="max-w-[78%] rounded-2xl bg-[var(--bg-subtle)] px-4 py-2.5 text-body text-[var(--text-primary)] whitespace-pre-wrap">
            {turn.userText}
          </div>
        </div>
      )}

      {/* Assistant turn output */}
      <div className="space-y-3">
        {isRunning || !finalItem ? (
          turn.items.length > 0 && <TurnItemList items={turn.items} />
        ) : (
          <>
            {processItems.length > 0 && (
              <Collapsible.Root
                open={processOpen}
                onOpenChange={setProcessOpen}
                className="border-b border-[var(--border-hairline)] pb-3"
              >
                <Collapsible.Trigger className="inline-flex items-center gap-1 rounded-lg py-0.5 text-ui text-[var(--text-muted)] transition-colors hover:text-[var(--text-secondary)]">
                  <span>{collapsedLabel}</span>
                  <ChevronRight
                    className={cn('h-3.5 w-3.5 transition-transform', processOpen && 'rotate-90')}
                  />
                </Collapsible.Trigger>
                <Collapsible.Content className="pt-3">
                  <TurnItemList items={processItems} />
                </Collapsible.Content>
              </Collapsible.Root>
            )}

            <MarkdownRenderer content={finalItem.text} />
          </>
        )}

        {/* Active pulsing progress indicator */}
        {isRunning && (
          <div className="flex items-center gap-2 border-t border-[var(--border-hairline)] pt-3 text-caption text-[var(--text-muted)]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>CARE 正在执行</span>
            <LiveElapsed startedAt={turn.startedAt} />
          </div>
        )}

        {/* Failure / RuntimeError block */}
        {(turn.failure || turn.runtimeError) && (
          <div className="flex items-start gap-2 rounded-xl border border-[#f2c6c6] bg-[#fff5f5] px-3.5 py-2.5 text-ui text-[var(--status-danger)]">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <div className="font-medium">
                {turn.failure?.failure_class || turn.runtimeError?.code}
              </div>
              <div className="mt-0.5">{turn.failure?.message || turn.runtimeError?.message}</div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
})

function WorkflowStageBar({
  workflowId,
  nodes
}: {
  workflowId: string | null
  nodes: ProjectedWorkflowNode[]
}): React.JSX.Element | null {
  if (nodes.length <= 1 && workflowId !== 'plan_and_execute') return null

  const displayNodes: ProjectedWorkflowNode[] =
    nodes.length > 0
      ? nodes
      : [
          { nodeId: 'planner', role: 'planner', adapter: null, status: 'running', summary: '' },
          {
            nodeId: 'executor',
            role: 'executor',
            adapter: null,
            status: 'interrupted',
            summary: ''
          }
        ]

  return (
    <div className="flex items-center gap-2 border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)]/40 px-6 py-2 text-caption">
      <span className="font-medium text-[var(--text-muted)]">{workflowId}</span>
      <span className="text-[var(--border-card)]">|</span>
      <div className="flex items-center gap-2 overflow-x-auto">
        {displayNodes.map((node, idx) => (
          <div key={node.nodeId} className="flex items-center gap-1.5">
            {idx > 0 && <ChevronRight className="h-3 w-3 text-[var(--text-muted)]" />}
            <span
              className={cn(
                'inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium',
                node.status === 'running' && 'bg-[var(--bg-active)] text-[var(--text-primary)]',
                node.status === 'completed' && 'text-[var(--status-success)]',
                node.status === 'interrupted' && 'bg-[#fff7e6] text-[var(--status-warning)]',
                (node.status === 'failed' || node.status === 'aborted') &&
                  'text-[var(--status-danger)]'
              )}
            >
              {node.status === 'running' ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : node.status === 'completed' ? (
                <CheckCircle2 className="h-3 w-3" />
              ) : (
                <CircleDot className="h-3 w-3" />
              )}
              <span>{node.nodeId}</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function ConversationView(): React.JSX.Element {
  const events = useSessionStore((s) => s.events)
  const activeRunId = useSessionStore((s) => s.activeRunId)
  const activeWorkflowId = useSessionStore((s) => s.activeWorkflowId)
  const threadState = useSessionStore((s) => s.threadState)
  const loadingSession = useSessionStore((s) => s.loadingSession)
  const error = useSessionStore((s) => s.error)

  const bottomRef = useRef<HTMLDivElement | null>(null)
  const timeline = useMemo(() => projectEventsToTimeline(events), [events])

  const isRunning = Boolean(activeRunId) || timeline.isRunning
  const pendingInterrupts =
    threadState?.interrupts && threadState.interrupts.length > 0
      ? threadState.interrupts
      : timeline.workflowInterrupts
  const isResumable = Boolean(threadState?.resumable) || timeline.workflowInterrupts.length > 0
  const isAwaitingInput =
    !isRunning && pendingInterrupts.some((item) => item.kind === 'awaiting_input')

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [events.length, pendingInterrupts.length])

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <WorkflowStageBar workflowId={activeWorkflowId} nodes={timeline.nodes} />

      {/* Scrollable conversation stream */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-[760px] space-y-8">
          {loadingSession ? (
            <div className="flex items-center justify-center gap-2 py-16 text-ui text-[var(--text-muted)]">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>正在加载历史会话...</span>
            </div>
          ) : (
            timeline.turns.map((turn) => <TurnItem key={turn.id} turn={turn} />)
          )}

          <InteractionBanner
            liveInteractions={timeline.liveInteractions}
            pendingInterrupts={pendingInterrupts}
            resumable={isResumable}
          />

          {error && (
            <div className="rounded-xl border border-[#f2c6c6] bg-[#fff5f5] px-3.5 py-2.5 text-ui text-[var(--status-danger)]">
              {error}
            </div>
          )}

          <div ref={bottomRef} className="h-4" />
        </div>
      </div>

      {/* Floating bottom composer */}
      <div className="shrink-0 px-6 pb-3 pt-1">
        <div className="mx-auto max-w-[760px]">
          <ComposerCard
            isRunning={isRunning}
            isAwaitingInput={isAwaitingInput}
            latestUsage={timeline.latestUsage}
          />
          <div className="mt-1.5 text-center text-caption text-[var(--text-muted)]">
            内容由 AI 生成，请注意核实
          </div>
        </div>
      </div>
    </div>
  )
}
