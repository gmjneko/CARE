import { useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import { AlertCircle, CheckCircle2, ChevronRight, Loader2, Terminal, Wrench } from 'lucide-react'
import type { ProjectedToolCall, ToolSummaryCounts } from '@renderer/sdk/event-projection'
import { toolRendererRegistry } from '@renderer/shell/registries'
import { cn } from '@renderer/ui/cn'

interface ToolGroupSummaryProps {
  toolCalls: ProjectedToolCall[]
  counts: ToolSummaryCounts
}

function buildSummaryLabel(counts: ToolSummaryCounts): string {
  const parts: string[] = []
  if (counts.actions > 0) parts.push(`已执行 ${counts.actions} 个操作`)
  if (counts.reads > 0) parts.push(`已读取 ${counts.reads} 个文件`)
  if (counts.commands > 0) parts.push(`已运行 ${counts.commands} 条命令`)
  return parts.join(' · ')
}

/**
 * Aggregated expandable tool activity row aligned with Screenshot 2.
 */
export function ToolGroupSummary({
  toolCalls,
  counts
}: ToolGroupSummaryProps): React.JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (toolCalls.length === 0) return null

  const hasRunning = toolCalls.some((t) => t.status === 'running')
  const hasError = toolCalls.some((t) => t.status === 'error')
  const customRenderers = toolRendererRegistry.list()

  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} className="my-2">
      <Collapsible.Trigger className="group inline-flex items-center gap-1.5 rounded-lg py-1 text-[12.5px] text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)]">
        {hasRunning ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--text-muted)]" />
        ) : hasError ? (
          <AlertCircle className="h-3.5 w-3.5 text-[var(--status-danger)]" />
        ) : (
          <Wrench className="h-3.5 w-3.5 text-[var(--text-muted)]" strokeWidth={1.65} />
        )}
        <span>{buildSummaryLabel(counts)}</span>
        <ChevronRight
          className={cn(
            'h-3.5 w-3.5 text-[var(--text-muted)] transition-transform',
            open && 'rotate-90'
          )}
        />
      </Collapsible.Trigger>

      <Collapsible.Content className="mt-1.5 space-y-2 border-l border-[var(--border-hairline)] pl-3">
        {toolCalls.map((call) => {
          const custom = customRenderers.find((r) => r.matches(call))
          if (custom) {
            const CustomComponent = custom.Component
            return <CustomComponent key={call.toolCallId} toolCall={call} />
          }
          return <DefaultToolCard key={call.toolCallId} call={call} />
        })}
      </Collapsible.Content>
    </Collapsible.Root>
  )
}

function DefaultToolCard({ call }: { call: ProjectedToolCall }): React.JSX.Element {
  const [expanded, setExpanded] = useState(call.status === 'running')
  const argPreview = Object.entries(call.arguments)
    .slice(0, 2)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ')

  return (
    <div className="overflow-hidden rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/40 text-xs">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left transition-colors hover:bg-[var(--bg-hover)]/60"
      >
        <div className="flex min-w-0 items-center gap-2">
          {call.status === 'running' ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--text-muted)]" />
          ) : call.status === 'error' ? (
            <AlertCircle className="h-3.5 w-3.5 shrink-0 text-[var(--status-danger)]" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-[var(--status-success)]" />
          )}
          <Terminal className="h-3 w-3 shrink-0 text-[var(--text-muted)]" />
          <span className="font-mono font-medium text-[var(--text-primary)]">{call.toolName}</span>
          {argPreview && (
            <span className="truncate font-mono text-[11px] text-[var(--text-muted)]">
              {argPreview}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2 text-[11px] text-[var(--text-muted)]">
          {call.durationMs !== null && <span>{(call.durationMs / 1000).toFixed(2)}s</span>}
          <ChevronRight
            className={cn('h-3.5 w-3.5 transition-transform', expanded && 'rotate-90')}
          />
        </div>
      </button>

      {expanded && (
        <div className="space-y-2 border-t border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2.5">
          {Object.keys(call.arguments).length > 0 && (
            <pre className="overflow-x-auto rounded-lg bg-[var(--bg-subtle)] p-2 font-mono text-[11.5px] text-[var(--text-secondary)]">
              {JSON.stringify(call.arguments, null, 2)}
            </pre>
          )}
          {call.output && (
            <pre
              className={cn(
                'max-h-[240px] overflow-auto rounded-lg p-2 font-mono text-[11.5px] whitespace-pre-wrap',
                call.status === 'error'
                  ? 'bg-[#fff5f5] text-[var(--status-danger)]'
                  : 'bg-[var(--bg-subtle)]/70 text-[var(--text-primary)]'
              )}
            >
              {call.output}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
