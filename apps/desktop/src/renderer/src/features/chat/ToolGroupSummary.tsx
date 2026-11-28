import { useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import {
  AlertCircle,
  Brain,
  ChevronRight,
  FileText,
  Loader2,
  SquareTerminal,
  Wrench
} from 'lucide-react'
import {
  formatDurationZh,
  type ProjectedToolCall,
  type ProjectedTurnItem,
  type ToolSummaryCounts
} from '@renderer/sdk/event-projection'
import { toolRendererRegistry } from '@renderer/shell/registries'
import { cn } from '@renderer/ui/cn'

export type ActivityStep = Extract<ProjectedTurnItem, { kind: 'thinking' | 'tool_group' }>

export function ThinkingBlock({
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
              ? `已思考 ${formatDurationZh(item.durationMs)}`
              : '已思考'}
        </span>
        <ChevronRight className={cn('care-chevron h-3.5 w-3.5', open && 'rotate-90')} />
      </Collapsible.Trigger>
      <Collapsible.Content className="care-collapsible">
        <div className="mt-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/50 px-3.5 py-2.5 text-ui text-[var(--text-secondary)] whitespace-pre-wrap">
          {item.text}
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  )
}

function buildClusterLabel(
  counts: ToolSummaryCounts,
  hasThinking: boolean,
  thinkingStreaming: boolean,
  runningCategories: Set<ProjectedToolCall['category']>
): string {
  const parts: string[] = []
  if (thinkingStreaming) {
    parts.push('正在思考...')
  } else if (hasThinking) {
    parts.push('已思考')
  }

  if (counts.commands > 0) {
    if (runningCategories.has('command')) {
      parts.push(
        counts.commands === 1 ? '正在运行命令...' : `正在运行命令（${counts.commands}）...`
      )
    } else {
      parts.push(counts.commands === 1 ? '运行了命令' : `运行了 ${counts.commands} 条命令`)
    }
  }
  if (counts.reads > 0) {
    if (runningCategories.has('read')) {
      parts.push(counts.reads === 1 ? '正在读取文件...' : `正在读取文件（${counts.reads}）...`)
    } else {
      parts.push(counts.reads === 1 ? '读取了文件' : `读取了 ${counts.reads} 个文件`)
    }
  }
  if (counts.actions > 0) {
    if (runningCategories.has('action')) {
      parts.push(counts.actions === 1 ? '正在执行操作...' : `正在执行操作（${counts.actions}）...`)
    } else {
      parts.push(counts.actions === 1 ? '执行了操作' : `执行了 ${counts.actions} 个操作`)
    }
  }
  return parts.join(' · ')
}

function formatCallPreview(call: ProjectedToolCall): string {
  const args = call.arguments
  if (typeof args.command === 'string' && args.command.trim()) {
    return args.command.replace(/\s+/g, ' ').trim()
  }
  const pathVal = args.path ?? args.file_path ?? args.pattern
  if (typeof pathVal === 'string' && pathVal.trim()) {
    return `${call.toolName} ${pathVal.trim()}`
  }
  const rest = Object.entries(args)
    .slice(0, 2)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ')
  return rest ? `${call.toolName} ${rest}` : call.toolName
}

function ToolCategoryIcon({
  category,
  status
}: {
  category: ProjectedToolCall['category']
  status: ProjectedToolCall['status']
}): React.JSX.Element {
  if (status === 'running') {
    return <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--text-muted)]" />
  }
  if (status === 'error') {
    return <AlertCircle className="h-3.5 w-3.5 shrink-0 text-[var(--status-danger)]" />
  }
  if (category === 'command') {
    return (
      <SquareTerminal
        className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]"
        strokeWidth={1.65}
      />
    )
  }
  if (category === 'read') {
    return <FileText className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" strokeWidth={1.65} />
  }
  return <Wrench className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" strokeWidth={1.65} />
}

/**
 * Unified collapsible cluster for consecutive silent thinking + tool steps between narration texts.
 */
export function ActivityCluster({ steps }: { steps: ActivityStep[] }): React.JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (steps.length === 0) return null

  if (steps.length === 1 && steps[0].kind === 'thinking') {
    return <ThinkingBlock item={steps[0]} />
  }

  const counts: ToolSummaryCounts = { actions: 0, reads: 0, commands: 0 }
  const runningCategories = new Set<ProjectedToolCall['category']>()
  let hasThinking = false
  let thinkingStreaming = false
  let hasError = false

  for (const step of steps) {
    if (step.kind === 'thinking') {
      hasThinking = true
      if (step.streaming) thinkingStreaming = true
    } else {
      counts.actions += step.counts.actions
      counts.reads += step.counts.reads
      counts.commands += step.counts.commands
      for (const call of step.toolCalls) {
        if (call.status === 'running') runningCategories.add(call.category)
        if (call.status === 'error') hasError = true
      }
    }
  }

  const isRunning = thinkingStreaming || runningCategories.size > 0
  const totalTools = counts.actions + counts.reads + counts.commands
  const customRenderers = toolRendererRegistry.list()

  const clusterIcon = isRunning ? (
    <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--text-muted)]" />
  ) : hasError ? (
    <AlertCircle className="h-3.5 w-3.5 text-[var(--status-danger)]" />
  ) : totalTools === 0 ? (
    <Brain className="h-3.5 w-3.5 text-[var(--text-muted)]" strokeWidth={1.65} />
  ) : counts.commands > 0 && counts.reads === 0 && counts.actions === 0 ? (
    <SquareTerminal className="h-3.5 w-3.5 text-[var(--text-muted)]" strokeWidth={1.65} />
  ) : counts.reads > 0 && counts.commands === 0 && counts.actions === 0 ? (
    <FileText className="h-3.5 w-3.5 text-[var(--text-muted)]" strokeWidth={1.65} />
  ) : (
    <Wrench className="h-3.5 w-3.5 text-[var(--text-muted)]" strokeWidth={1.65} />
  )

  return (
    <Collapsible.Root open={open} onOpenChange={setOpen}>
      <Collapsible.Trigger className="group inline-flex items-center gap-1.5 rounded-lg py-0.5 text-ui text-[var(--text-muted)] transition-colors hover:text-[var(--text-secondary)]">
        {clusterIcon}
        <span>{buildClusterLabel(counts, hasThinking, thinkingStreaming, runningCategories)}</span>
        <ChevronRight
          className={cn('care-chevron h-3.5 w-3.5 text-[var(--text-muted)]', open && 'rotate-90')}
        />
      </Collapsible.Trigger>

      <Collapsible.Content className="care-collapsible">
        <div className="ml-1.5 mt-1.5 space-y-1.5 border-l border-[var(--border-hairline)] pl-3">
          {steps.map((step) => {
            if (step.kind === 'thinking') {
              return <ThinkingBlock key={step.id} item={step} />
            }
            return step.toolCalls.map((call) => {
              const custom = customRenderers.find((r) => r.matches(call))
              if (custom) {
                const CustomComponent = custom.Component
                return <CustomComponent key={call.toolCallId} toolCall={call} />
              }
              return <DefaultToolRow key={call.toolCallId} call={call} />
            })
          })}
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  )
}

function DefaultToolRow({ call }: { call: ProjectedToolCall }): React.JSX.Element {
  const [expanded, setExpanded] = useState(call.status === 'running')
  const preview = formatCallPreview(call)

  return (
    <Collapsible.Root open={expanded} onOpenChange={setExpanded} className="text-ui">
      <Collapsible.Trigger className="flex w-full items-center justify-between gap-2 rounded-lg px-1.5 py-1 text-left text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)]/70 hover:text-[var(--text-secondary)]">
        <div className="flex min-w-0 items-center gap-2">
          <ToolCategoryIcon category={call.category} status={call.status} />
          <span className="truncate font-mono text-code">{preview}</span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5 text-caption text-[var(--text-muted)]">
          {call.durationMs !== null && <span>{(call.durationMs / 1000).toFixed(2)}s</span>}
          <ChevronRight className={cn('care-chevron h-3 w-3', expanded && 'rotate-90')} />
        </div>
      </Collapsible.Trigger>

      <Collapsible.Content className="care-collapsible">
        <div className="mt-1.5 space-y-2 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2.5">
          {Object.keys(call.arguments).length > 0 && (
            <pre className="overflow-x-auto rounded-lg bg-[var(--bg-subtle)] p-2 font-mono text-code text-[var(--text-secondary)]">
              {JSON.stringify(call.arguments, null, 2)}
            </pre>
          )}
          {call.output && (
            <pre
              className={cn(
                'max-h-[240px] overflow-auto rounded-lg p-2 font-mono text-code whitespace-pre-wrap',
                call.status === 'error'
                  ? 'bg-[#fff5f5] text-[var(--status-danger)]'
                  : 'bg-[var(--bg-subtle)]/70 text-[var(--text-primary)]'
              )}
            >
              {call.output}
            </pre>
          )}
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  )
}
