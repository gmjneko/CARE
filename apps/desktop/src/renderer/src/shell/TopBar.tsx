import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import {
  ChevronDown,
  Folder,
  FolderOpen,
  Laptop,
  PanelLeft,
  PanelRight,
  Terminal,
  Workflow
} from 'lucide-react'
import type { ExternalAppKind } from '@shared/ipc-contracts'
import { formatWorkflowLabel } from '@renderer/sdk/event-projection'
import { cn } from '@renderer/ui/cn'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { pageRegistry } from './registries'

const EXTERNAL_APPS: Array<{ id: ExternalAppKind; label: string }> = [
  { id: 'vscode', label: '在 VS Code 中打开' },
  { id: 'cursor', label: '在 Cursor 中打开' },
  { id: 'idea', label: '在 IntelliJ IDEA 中打开' },
  { id: 'terminal', label: '在终端中打开' },
  { id: 'finder', label: '在访达中显示' }
]

function FloatingCardIcon({ className }: { className?: string }): React.JSX.Element {
  return (
    <svg
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <rect x="3.25" y="2.25" width="11.5" height="13.5" rx="3" />
      <line x1="6.5" y1="7" x2="11.5" y2="7" />
      <line x1="6.5" y1="10.75" x2="9.75" y2="10.75" />
    </svg>
  )
}

export function TopBar(): React.JSX.Element {
  const sidebarOpen = useWorkbenchStore((s) => s.sidebarOpen)
  const toggleSidebar = useWorkbenchStore((s) => s.toggleSidebar)
  const inspectorMode = useWorkbenchStore((s) => s.inspectorMode)
  const toggleFloatingInspector = useWorkbenchStore((s) => s.toggleFloatingInspector)
  const toggleRightPanel = useWorkbenchStore((s) => s.toggleRightPanel)
  const activePageId = useWorkbenchStore((s) => s.activePageId)
  const selectedWorkspaceDir = useWorkbenchStore((s) => s.selectedWorkspaceDir)

  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  const activeWorkspaceDir = useSessionStore((s) => s.activeWorkspaceDir)
  const activeWorkflowId = useSessionStore((s) => s.activeWorkflowId)
  const threads = useSessionStore((s) => s.threads)

  const activeThread = threads.find((t) => t.thread_id === activeThreadId) ?? null
  const activePage = pageRegistry.get(activePageId)
  const workspaceDir = activeWorkspaceDir || selectedWorkspaceDir
  const workflowId = activeWorkflowId || activeThread?.workflow_id || null
  const isMac = window.careDesktop?.platform === 'darwin'
  const showChatHeader = activePageId === 'chat' && Boolean(activeThreadId)

  const handleOpenExternal = (app: ExternalAppKind): void => {
    if (!workspaceDir) return
    void window.careDesktop.openInExternalApp(workspaceDir, app).catch(() => undefined)
  }

  return (
    <header
      className={cn(
        'app-drag flex h-[52px] shrink-0 items-center justify-between border-b border-transparent px-4 select-none',
        !sidebarOpen && isMac ? 'pl-[84px]' : 'pl-4',
        showChatHeader && 'border-[var(--border-hairline)]/70'
      )}
    >
      {/* Left: sidebar toggle when collapsed + active title */}
      <div className="flex min-w-0 items-center gap-2">
        {!sidebarOpen && (
          <button
            type="button"
            onClick={toggleSidebar}
            title="展开侧边栏"
            className="app-no-drag inline-flex h-7 w-7 items-center justify-center rounded-lg text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
          >
            <PanelLeft className="h-4 w-4" strokeWidth={1.7} />
          </button>
        )}

        {showChatHeader ? (
          <div className="flex min-w-0 items-center gap-2">
            <Folder className="h-4 w-4 shrink-0 text-[var(--text-muted)]" strokeWidth={1.7} />
            <span className="truncate text-ui font-medium text-[var(--text-primary)]">
              {activeThread?.title || '新任务'}
            </span>
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-[var(--bg-subtle)] px-2 py-0.5 text-caption text-[var(--text-secondary)]">
              <Laptop className="h-3 w-3" strokeWidth={1.7} />
              本地
            </span>
            {workflowId && workflowId !== 'single_agent' && (
              <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-[var(--bg-subtle)] px-2 py-0.5 text-caption text-[var(--text-secondary)]">
                <Workflow className="h-3 w-3" strokeWidth={1.7} />
                {formatWorkflowLabel(workflowId)}
              </span>
            )}
          </div>
        ) : activePageId !== 'chat' && activePage ? (
          <div className="flex items-center gap-2 text-ui font-medium text-[var(--text-primary)]">
            <activePage.icon className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.7} />
            <span>{activePage.title}</span>
          </div>
        ) : null}
      </div>

      {/* Right: Open external app + Inspector drawer toggle */}
      <div className="app-no-drag flex items-center gap-1.5">
        {showChatHeader && workspaceDir && (
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2.5 text-ui text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-hover)]"
              >
                <FolderOpen className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.7} />
                <span>打开位置</span>
                <ChevronDown className="h-3.5 w-3.5 text-[var(--text-muted)]" />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                align="end"
                sideOffset={6}
                className="z-50 min-w-[190px] rounded-xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-1.5 shadow-[var(--shadow-floating)]"
              >
                {EXTERNAL_APPS.map((item) => (
                  <DropdownMenu.Item
                    key={item.id}
                    onSelect={() => handleOpenExternal(item.id)}
                    className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-ui text-[var(--text-primary)] outline-none hover:bg-[var(--bg-hover)]"
                  >
                    {item.id === 'terminal' ? (
                      <Terminal className="h-4 w-4 text-[var(--text-secondary)]" />
                    ) : (
                      <FolderOpen className="h-4 w-4 text-[var(--text-secondary)]" />
                    )}
                    {item.label}
                  </DropdownMenu.Item>
                ))}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        )}

        {activePageId === 'chat' && Boolean(activeThreadId) && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={toggleFloatingInspector}
              title={inspectorMode === 'floating' ? '隐藏悬浮概览卡' : '显示悬浮概览卡'}
              className={cn(
                'inline-flex h-8 w-8 items-center justify-center rounded-lg transition-colors',
                inspectorMode === 'floating'
                  ? 'bg-[var(--bg-hover)] text-[var(--text-primary)]'
                  : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]'
              )}
            >
              <FloatingCardIcon className="h-[17px] w-[17px]" />
            </button>
            <button
              type="button"
              onClick={toggleRightPanel}
              title={inspectorMode === 'panel' ? '收起右侧详情边栏' : '展开右侧详情边栏'}
              className={cn(
                'inline-flex h-8 w-8 items-center justify-center rounded-lg transition-colors',
                inspectorMode === 'panel'
                  ? 'bg-[var(--bg-hover)] text-[var(--text-primary)]'
                  : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]'
              )}
            >
              <PanelRight className="h-4 w-4" strokeWidth={1.7} />
            </button>
          </div>
        )}
      </div>
    </header>
  )
}
