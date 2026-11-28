import type { ReactNode } from 'react'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { InspectorDrawer } from './InspectorDrawer'
import { pageRegistry } from './registries'
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
  const activePageId = useWorkbenchStore((s) => s.activePageId)
  const activePage = pageRegistry.get(activePageId)
  const ActivePageComponent = activePage?.Component

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[var(--bg-sidebar)] text-[var(--text-primary)]">
      {sidebarOpen && <Sidebar />}

      {/* Main white canvas */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-[var(--bg-canvas)]">
        <TopBar />
        <div className="relative flex min-h-0 flex-1 overflow-hidden">
          <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
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
