import { useMemo, useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import { ArrowLeft, ChevronDown, PanelRight, X } from 'lucide-react'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { cn } from '@renderer/ui/cn'
import type { WorkbenchContext } from './contracts'
import { getVisibleInspectorWidgets } from './registries'

/**
 * Right-hand inspector supporting two distinct visual states controlled by TopBar's two icons:
 * - `'floating'`: compact content-hugging floating white card (~264px) stacking registered widgets
 * - `'panel'`: full-height docked right side panel (~520px) for detailed inspection (Git Diff,
 *   file previews, or expanded widget view)
 */
export function InspectorDrawer(): React.JSX.Element | null {
  const inspectorMode = useWorkbenchStore((s) => s.inspectorMode)
  const inspectorDetail = useWorkbenchStore((s) => s.inspectorDetail)
  const openInspectorDetail = useWorkbenchStore((s) => s.openInspectorDetail)
  const closeInspectorDetail = useWorkbenchStore((s) => s.closeInspectorDetail)
  const setInspectorMode = useWorkbenchStore((s) => s.setInspectorMode)

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

  if (inspectorMode === 'closed' || !activeThreadId) {
    return null
  }

  const renderWidgetStack = (): React.JSX.Element => (
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
            onOpenChange={(next) => setOpenSections((prev) => ({ ...prev, [widget.id]: next }))}
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
  )

  // Mode 1: Full-height docked right side panel (PanelRight icon)
  if (inspectorMode === 'panel') {
    return (
      <aside className="flex h-full w-[480px] max-w-[52vw] shrink-0 flex-col border-l border-[var(--border-hairline)] bg-[var(--bg-surface)]">
        <div className="flex items-center justify-between border-b border-[var(--border-hairline)] px-3.5 py-2.5">
          <div className="flex min-w-0 items-center gap-2">
            {inspectorDetail ? (
              <>
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
              </>
            ) : (
              <span className="text-ui font-semibold text-[var(--text-primary)]">工作区详情</span>
            )}
          </div>
          <button
            type="button"
            onClick={() => setInspectorMode('closed')}
            title="收起右侧边栏"
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          {inspectorDetail ? (
            <div className="flex-1 overflow-y-auto">{inspectorDetail.render()}</div>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
              <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-2xl border border-[var(--border-card)] bg-[var(--bg-subtle)]/70 text-[var(--text-muted)]">
                <PanelRight className="h-5 w-5" strokeWidth={1.6} />
              </div>
              <div className="text-ui font-medium text-[var(--text-primary)]">右侧扩展工作区</div>
              <p className="mt-1.5 max-w-[260px] text-caption text-[var(--text-muted)]">
                点击悬浮卡中的代码变更或项目文件将在此展开预览；后续也将承接终端与产物预览插件。
              </p>
            </div>
          )}
        </div>
      </aside>
    )
  }

  // Mode 2: Compact content-hugging floating overview card (FloatingCard icon)
  return (
    <aside className="my-3 mr-3.5 flex max-h-[calc(100%-24px)] w-[264px] shrink-0 self-start flex-col">
      <div className="flex max-h-full flex-col overflow-hidden rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)]/95 shadow-[var(--shadow-floating)] backdrop-blur-md">
        {renderWidgetStack()}
      </div>
    </aside>
  )
}
