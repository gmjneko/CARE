import { useMemo, useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import { ArrowLeft, ChevronDown, X } from 'lucide-react'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { cn } from '@renderer/ui/cn'
import type { WorkbenchContext } from './contracts'
import { getVisibleInspectorWidgets } from './registries'

/**
 * Floating right-hand inspector panel supporting two seamless modes:
 * - Compact stacked widgets (~280px, Screenshots 1 & 2)
 * - In-place widened detail view (~600px) when any widget calls openDetail() (e.g. Git Diff)
 */
export function InspectorDrawer(): React.JSX.Element | null {
  const inspectorOpen = useWorkbenchStore((s) => s.inspectorOpen)
  const inspectorDetail = useWorkbenchStore((s) => s.inspectorDetail)
  const openInspectorDetail = useWorkbenchStore((s) => s.openInspectorDetail)
  const closeInspectorDetail = useWorkbenchStore((s) => s.closeInspectorDetail)
  const setInspectorOpen = useWorkbenchStore((s) => s.setInspectorOpen)

  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  const activeWorkspaceDir = useSessionStore((s) => s.activeWorkspaceDir)
  const activeWorkflowId = useSessionStore((s) => s.activeWorkflowId)

  const [openSections, setOpenSections] = useState<Record<string, boolean>>({})

  const ctx: WorkbenchContext = useMemo(
    () => ({
      mode: 'local',
      activeThreadId,
      activeWorkspaceDir,
      activeWorkflowId
    }),
    [activeThreadId, activeWorkspaceDir, activeWorkflowId]
  )

  const widgets = useMemo(() => getVisibleInspectorWidgets(ctx), [ctx])

  if (!inspectorOpen || !activeThreadId) {
    return null
  }

  const isDetailMode = inspectorDetail !== null

  return (
    <aside
      className={cn(
        'flex shrink-0 flex-col p-2.5 pl-0 transition-[width] duration-200 ease-out',
        isDetailMode ? 'w-[580px] max-w-[55vw]' : 'w-[288px]'
      )}
    >
      <div className="flex h-full flex-col overflow-hidden rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] shadow-[var(--shadow-floating)]">
        {isDetailMode ? (
          <>
            <div className="flex items-center justify-between border-b border-[var(--border-hairline)] px-3.5 py-2.5">
              <div className="flex min-w-0 items-center gap-2">
                <button
                  type="button"
                  onClick={closeInspectorDetail}
                  className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-ui font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                >
                  <ArrowLeft className="h-3.5 w-3.5" />
                  <span>返回概览</span>
                </button>
                <span className="h-3.5 w-px bg-[var(--border-hairline)]" />
                <div className="min-w-0">
                  <div className="truncate text-ui font-medium text-[var(--text-primary)]">
                    {inspectorDetail.title}
                  </div>
                  {inspectorDetail.subtitle && (
                    <div className="truncate text-caption text-[var(--text-muted)]">
                      {inspectorDetail.subtitle}
                    </div>
                  )}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setInspectorOpen(false)}
                title="关闭右侧面板"
                className="inline-flex h-6 w-6 items-center justify-center rounded-md text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">{inspectorDetail.render()}</div>
          </>
        ) : (
          <div className="flex-1 divide-y divide-[var(--border-hairline)] overflow-y-auto px-3.5 py-2">
            {widgets.map((widget) => {
              const isOpen = openSections[widget.id] ?? widget.defaultOpen ?? true
              const badge = widget.headerBadge?.({
                threadId: activeThreadId,
                workspaceDir: activeWorkspaceDir,
                workflowId: activeWorkflowId
              })
              const WidgetComponent = widget.Component
              return (
                <Collapsible.Root
                  key={widget.id}
                  open={isOpen}
                  onOpenChange={(next) =>
                    setOpenSections((prev) => ({ ...prev, [widget.id]: next }))
                  }
                  className="py-2.5 first:pt-1"
                >
                  <Collapsible.Trigger className="flex w-full items-center justify-between py-1 text-left">
                    <span className="inline-flex items-center gap-1.5 text-ui font-semibold text-[var(--text-primary)]">
                      {widget.title}
                      <ChevronDown
                        className={cn(
                          'h-3.5 w-3.5 text-[var(--text-muted)] transition-transform',
                          !isOpen && '-rotate-90'
                        )}
                      />
                    </span>
                    {badge !== null && badge !== undefined && (
                      <span className="rounded-md bg-[var(--bg-subtle)] px-1.5 py-0.5 text-caption font-medium text-[var(--text-secondary)]">
                        {badge}
                      </span>
                    )}
                  </Collapsible.Trigger>
                  <Collapsible.Content className="pt-2">
                    <WidgetComponent
                      threadId={activeThreadId}
                      workspaceDir={activeWorkspaceDir}
                      workflowId={activeWorkflowId}
                      openDetail={openInspectorDetail}
                      closeDetail={closeInspectorDetail}
                    />
                  </Collapsible.Content>
                </Collapsible.Root>
              )
            })}
          </div>
        )}
      </div>
    </aside>
  )
}
