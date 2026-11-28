import { useMemo, useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import { ArrowLeft, ChevronDown, PanelRight, X } from 'lucide-react'
import { useSessionStore } from '@renderer/stores/session-store'
import {
  DEFAULT_RIGHT_PANEL_WIDTH,
  MAX_RIGHT_PANEL_WIDTH,
  MIN_RIGHT_PANEL_WIDTH,
  useWorkbenchStore
} from '@renderer/stores/workbench-store'
import { cn } from '@renderer/ui/cn'
import type { WorkbenchContext } from './contracts'
import { getVisibleInspectorWidgets } from './registries'
import { ResizeHandle } from './ResizeHandle'

/**
 * Right-hand inspector with continuous layout-squeezing transitions:
 * - `'floating'`: nimble spring-popped floating card (~264px) whose layout track smoothly
 *   expands/collapses so the main conversation area squeezes continuously.
 * - `'panel'`: crisp, continuous sliding docked right side panel (~480px) with no fade/blur.
 */
export function InspectorDrawer(): React.JSX.Element | null {
  const inspectorMode = useWorkbenchStore((s) => s.inspectorMode)
  const rightPanelWidth = useWorkbenchStore((s) => s.rightPanelWidth)
  const setRightPanelWidth = useWorkbenchStore((s) => s.setRightPanelWidth)
  const inspectorDetail = useWorkbenchStore((s) => s.inspectorDetail)
  const openInspectorDetail = useWorkbenchStore((s) => s.openInspectorDetail)
  const closeInspectorDetail = useWorkbenchStore((s) => s.closeInspectorDetail)
  const setInspectorMode = useWorkbenchStore((s) => s.setInspectorMode)

  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  const activeWorkspaceDir = useSessionStore((s) => s.activeWorkspaceDir)
  const activeWorkflowId = useSessionStore((s) => s.activeWorkflowId)

  const [openSections, setOpenSections] = useState<Record<string, boolean>>({})
  const [resizingPanel, setResizingPanel] = useState(false)

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

  if (!activeThreadId) {
    return null
  }

  const isFloating = inspectorMode === 'floating'
  const isPanel = inspectorMode === 'panel'

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
                    'care-chevron h-3.5 w-3.5 text-[var(--text-muted)]',
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
            <Collapsible.Content className="care-collapsible">
              <div className="pt-2">
                <WidgetComponent
                  threadId={activeThreadId}
                  workspaceDir={activeWorkspaceDir}
                  workflowId={activeWorkflowId}
                  openDetail={openInspectorDetail}
                  closeDetail={closeInspectorDetail}
                />
              </div>
            </Collapsible.Content>
          </Collapsible.Root>
        )
      })}
    </div>
  )

  return (
    <>
      {/* Track 1: Floating overview card — continuous width squeeze + springy nimble card reveal */}
      <div
        aria-hidden={!isFloating}
        className={cn(
          'care-width-track flex h-full shrink-0 items-start justify-end',
          isFloating ? 'w-[288px] overflow-visible' : 'pointer-events-none w-0 overflow-hidden'
        )}
      >
        <aside
          className={cn(
            'care-floating-card my-3 mr-3.5 flex max-h-[calc(100%-24px)] w-[264px] shrink-0 origin-top-right flex-col',
            isFloating ? 'care-floating-card-open' : 'care-floating-card-closed'
          )}
        >
          <div className="flex max-h-full flex-col overflow-hidden rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] shadow-[var(--shadow-floating)]">
            {renderWidgetStack()}
          </div>
        </aside>
      </div>

      {/* Track 2: Docked right side panel — crisp continuous push-in/out + manual edge resize */}
      <div
        aria-hidden={!isPanel}
        style={{ width: isPanel ? rightPanelWidth : 0 }}
        className={cn(
          'relative h-full max-w-[60vw] shrink-0',
          !resizingPanel && 'care-width-track',
          !isPanel && 'pointer-events-none'
        )}
      >
        {isPanel && (
          <ResizeHandle
            side="right"
            currentWidth={rightPanelWidth}
            minWidth={MIN_RIGHT_PANEL_WIDTH}
            maxWidth={MAX_RIGHT_PANEL_WIDTH}
            defaultWidth={DEFAULT_RIGHT_PANEL_WIDTH}
            onResize={setRightPanelWidth}
            onResizingChange={setResizingPanel}
          />
        )}
        <div className="h-full w-full overflow-hidden">
          <aside
            style={{ width: rightPanelWidth }}
            className="flex h-full max-w-[60vw] flex-col border-l border-[var(--border-hairline)] bg-[var(--bg-surface)]"
          >
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
                  <span className="text-ui font-semibold text-[var(--text-primary)]">
                    工作区详情
                  </span>
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

            <div
              key={inspectorDetail?.id ?? 'empty'}
              className="care-section-blur flex min-h-0 flex-1 flex-col overflow-hidden"
            >
              {inspectorDetail ? (
                <div className="flex-1 overflow-y-auto">{inspectorDetail.render()}</div>
              ) : (
                <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
                  <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-2xl border border-[var(--border-card)] bg-[var(--bg-subtle)]/70 text-[var(--text-muted)]">
                    <PanelRight className="h-5 w-5" strokeWidth={1.6} />
                  </div>
                  <div className="text-ui font-medium text-[var(--text-primary)]">
                    右侧扩展工作区
                  </div>
                  <p className="mt-1.5 max-w-[260px] text-caption text-[var(--text-muted)]">
                    点击悬浮卡中的代码变更或项目文件将在此展开预览；后续也将承接终端与产物预览插件。
                  </p>
                </div>
              )}
            </div>
          </aside>
        </div>
      </div>
    </>
  )
}
