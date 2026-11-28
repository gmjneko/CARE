import { useState, type ReactNode } from 'react'
import {
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  useWorkbenchStore
} from '@renderer/stores/workbench-store'
import { cn } from '@renderer/ui/cn'
import { InspectorDrawer } from './InspectorDrawer'
import { pageRegistry } from './registries'
import { ResizeHandle } from './ResizeHandle'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'

interface WorkbenchShellProps {
  /** Optional global overlays (Settings modal, Notifications drawer) injected by App bootstrap. */
  overlays?: ReactNode
}

/**
 * Business-agnostic workbench frame.
 * Renders the registered active PageContribution and InspectorWidgetContribution stack without
 * directly importing any feature module.
 */
export function WorkbenchShell({ overlays }: WorkbenchShellProps): React.JSX.Element {
  const sidebarOpen = useWorkbenchStore((s) => s.sidebarOpen)
  const sidebarWidth = useWorkbenchStore((s) => s.sidebarWidth)
  const setSidebarWidth = useWorkbenchStore((s) => s.setSidebarWidth)
  const activePageId = useWorkbenchStore((s) => s.activePageId)
  const activePage = pageRegistry.get(activePageId)
  const ActivePageComponent = activePage?.Component
  const [resizingSidebar, setResizingSidebar] = useState(false)

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[var(--bg-sidebar)] text-[var(--text-primary)]">
      <div
        style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        className={cn(
          'relative h-full shrink-0',
          !resizingSidebar && 'care-width-track',
          !sidebarOpen && 'pointer-events-none'
        )}
      >
        <div className="h-full w-full overflow-hidden">
          <Sidebar width={sidebarWidth} />
        </div>
        {sidebarOpen && (
          <ResizeHandle
            side="left"
            currentWidth={sidebarWidth}
            minWidth={MIN_SIDEBAR_WIDTH}
            maxWidth={MAX_SIDEBAR_WIDTH}
            defaultWidth={DEFAULT_SIDEBAR_WIDTH}
            onResize={setSidebarWidth}
            onResizingChange={setResizingSidebar}
          />
        )}
      </div>

      {/* Main white canvas */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-[var(--bg-canvas)]">
        <TopBar />
        <div className="relative flex min-h-0 flex-1 overflow-hidden">
          <main
            key={activePageId}
            className="care-page-enter flex min-w-0 flex-1 flex-col overflow-hidden"
          >
            {ActivePageComponent ? (
              <ActivePageComponent />
            ) : (
              <div className="flex flex-1 items-center justify-center text-ui text-[var(--text-muted)]">
                未找到已注册页面：{activePageId}
              </div>
            )}
          </main>
          {activePageId === 'chat' && <InspectorDrawer />}
        </div>
      </div>

      {overlays}
    </div>
  )
}
