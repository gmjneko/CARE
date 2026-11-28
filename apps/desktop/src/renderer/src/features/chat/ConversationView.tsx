import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import { AlertCircle, ChevronRight, Loader2 } from 'lucide-react'
import {
  formatDurationZh,
  formatStageLabel,
  projectEventsToTimeline,
  type ProjectedTurn,
  type ProjectedTurnItem
} from '@renderer/sdk/event-projection'
import { useSessionStore } from '@renderer/stores/session-store'
import { cn } from '@renderer/ui/cn'
import { MarkdownRenderer } from '@renderer/ui/markdown/MarkdownRenderer'
import { ComposerCard } from './ComposerCard'
import { InteractionBanner } from './InteractionBanner'
import { ActivityCluster, type ActivityStep } from './ToolGroupSummary'

type NonStageItem = Exclude<ProjectedTurnItem, { kind: 'stage' }>

type GroupedRenderBlock =
  | { id: string; kind: 'activity'; steps: ActivityStep[] }
  | Extract<NonStageItem, { kind: 'text' | 'notice' }>

interface StageSegment {
  id: string
  stage: Extract<ProjectedTurnItem, { kind: 'stage' }> | null
  items: NonStageItem[]
}

function splitByStage(items: ProjectedTurnItem[]): StageSegment[] {
  const segments: StageSegment[] = []
  let current: StageSegment | null = null

  for (const item of items) {
    if (item.kind === 'stage') {
      current = { id: item.id, stage: item, items: [] }
      segments.push(current)
    } else {
      if (!current) {
        current = { id: 'default', stage: null, items: [] }
        segments.push(current)
      }
      current.items.push(item)
    }
  }

  return segments
}

/**
 * Merge consecutive silent thinking + tool_group steps into a single collapsible ActivityCluster,
 * while preserving exact chronological interleaving around visible narration texts and notices.
 */
function groupConsecutiveActivity(items: NonStageItem[]): GroupedRenderBlock[] {
  const blocks: GroupedRenderBlock[] = []
  for (const item of items) {
    if (item.kind === 'thinking' || item.kind === 'tool_group') {
      const last = blocks[blocks.length - 1]
      if (last && last.kind === 'activity') {
        last.steps.push(item)
      } else {
        blocks.push({ id: `cluster:${item.id}`, kind: 'activity', steps: [item] })
      }
    } else {
      blocks.push(item)
    }
  }
  return blocks
}

function LiveElapsed({ startedAt }: { startedAt: string }): React.JSX.Element {
  const [elapsed, setElapsed] = useState(() => Math.max(0, Date.now() - Date.parse(startedAt)))

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsed(Math.max(0, Date.now() - Date.parse(startedAt)))
    }, 1000)
    return () => clearInterval(timer)
  }, [startedAt])

  return <span>{formatDurationZh(elapsed)}</span>
}

function StageDivider({
  stage
}: {
  stage: Extract<ProjectedTurnItem, { kind: 'stage' }>
}): React.JSX.Element {
  return (
    <div className="flex items-center gap-3 py-1 select-none">
      <div className="h-px flex-1 bg-[var(--border-hairline)]" />
      <span className="text-caption font-medium text-[var(--text-muted)]">
        {formatStageLabel(stage.nodeId, stage.role)}
      </span>
      <div className="h-px flex-1 bg-[var(--border-hairline)]" />
    </div>
  )
}

function TurnItemList({ items }: { items: NonStageItem[] }): React.JSX.Element {
  const blocks = useMemo(() => groupConsecutiveActivity(items), [items])

  return (
    <div className="space-y-3">
      {blocks.map((block) => {
        switch (block.kind) {
          case 'activity':
            return <ActivityCluster key={block.id} steps={block.steps} />
          case 'text':
            return <MarkdownRenderer key={block.id} content={block.text} />
          case 'notice':
            return (
              <div key={block.id} className="flex flex-wrap gap-1.5">
                <span className="rounded-full bg-[var(--bg-subtle)] px-2.5 py-0.5 text-caption text-[var(--text-muted)]">
                  {block.text}
                </span>
              </div>
            )
        }
      })}
    </div>
  )
}

function StageSegmentView({
  segment,
  isRunning,
  durationMs
}: {
  segment: StageSegment
  isRunning: boolean
  durationMs: number | null
}): React.JSX.Element {
  const [processOpen, setProcessOpen] = useState(false)
  const lastItem = segment.items[segment.items.length - 1]
  const hasFinalReply = !isRunning && lastItem?.kind === 'text'
  const processItems = hasFinalReply ? segment.items.slice(0, -1) : segment.items
  const finalItem = hasFinalReply ? lastItem : null

  const durationText = formatDurationZh(durationMs)
  const collapsedLabel = durationText ? `用时 ${durationText}` : '执行过程'

  return (
    <div className="space-y-3">
      {segment.stage && <StageDivider stage={segment.stage} />}

      {isRunning || !finalItem ? (
        segment.items.length > 0 && <TurnItemList items={segment.items} />
      ) : (
        <>
          {processItems.length > 0 && (
            <Collapsible.Root
              open={processOpen}
              onOpenChange={setProcessOpen}
              className="border-b border-[var(--border-hairline)] pb-3"
            >
              <Collapsible.Trigger className="inline-flex items-center gap-1 rounded-lg py-0.5 text-body text-[var(--text-muted)] transition-colors hover:text-[var(--text-secondary)]">
                <span>{collapsedLabel}</span>
                <ChevronRight className={cn('care-chevron h-4 w-4', processOpen && 'rotate-90')} />
              </Collapsible.Trigger>
              <Collapsible.Content className="care-collapsible">
                <div className="pt-3">
                  <TurnItemList items={processItems} />
                </div>
              </Collapsible.Content>
            </Collapsible.Root>
          )}

          <MarkdownRenderer content={finalItem.text} />
        </>
      )}
    </div>
  )
}

const TurnItem = memo(function TurnItem({ turn }: { turn: ProjectedTurn }): React.JSX.Element {
  const isRunning = turn.status === 'running'
  const segments = useMemo(() => splitByStage(turn.items), [turn.items])

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
      <div className="space-y-4">
        {segments.map((segment, idx) => (
          <StageSegmentView
            key={segment.id}
            segment={segment}
            isRunning={isRunning && idx === segments.length - 1}
            durationMs={segments.length === 1 ? turn.durationMs : null}
          />
        ))}

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

export function ConversationView(): React.JSX.Element {
  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  const events = useSessionStore((s) => s.events)
  const activeRunId = useSessionStore((s) => s.activeRunId)
  const threadState = useSessionStore((s) => s.threadState)
  const loadingSession = useSessionStore((s) => s.loadingSession)
  const error = useSessionStore((s) => s.error)

  const scrollContainerRef = useRef<HTMLDivElement | null>(null)
  const positionedThreadRef = useRef<string | null>(null)
  const timeline = useMemo(() => projectEventsToTimeline(events), [events])

  const isRunning = Boolean(activeRunId) || timeline.isRunning
  const pendingInterrupts =
    threadState?.interrupts && threadState.interrupts.length > 0
      ? threadState.interrupts
      : timeline.workflowInterrupts
  const isResumable = Boolean(threadState?.resumable) || timeline.workflowInterrupts.length > 0
  const isAwaitingInput =
    !isRunning && pendingInterrupts.some((item) => item.kind === 'awaiting_input')

  // Instant bottom-snap before paint on thread switch; smooth follow only for live updates near bottom.
  useLayoutEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    if (loadingSession) {
      positionedThreadRef.current = null
      return
    }
    if (positionedThreadRef.current !== activeThreadId) {
      el.scrollTop = el.scrollHeight
      positionedThreadRef.current = activeThreadId
      return
    }
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    if (distanceFromBottom < 180) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    }
  }, [activeThreadId, loadingSession, events.length, pendingInterrupts.length])

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      {/* Scrollable conversation stream (symmetric scrollbar gutters keep 0px alignment with composer) */}
      <div
        ref={scrollContainerRef}
        className="flex-1 overflow-y-auto px-6 py-6 [scrollbar-gutter:stable_both-edges]"
      >
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

          <div className="h-4" />
        </div>
      </div>

      {/* Floating bottom composer (30px horizontal padding matches 24px + 6px scrollbar gutter above) */}
      <div className="shrink-0 px-[30px] pb-3 pt-1">
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
