import { useMemo, useState } from 'react'
import * as Collapsible from '@radix-ui/react-collapsible'
import {
  AlertCircle,
  Bell,
  ChevronDown,
  ChevronRight,
  Folder,
  FolderPlus,
  Loader2,
  PanelLeft,
  Search,
  Settings,
  X
} from 'lucide-react'
import type { ThreadSummary } from '@renderer/sdk/runtime-types'
import { useRuntimeStore } from '@renderer/stores/runtime-store'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { cn } from '@renderer/ui/cn'
import type { WorkbenchContext } from './contracts'
import { getVisiblePages } from './registries'

function baseName(dirPath: string): string {
  const parts = dirPath.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || dirPath
}

export function Sidebar(): React.JSX.Element {
  const activePageId = useWorkbenchStore((s) => s.activePageId)
  const setActivePage = useWorkbenchStore((s) => s.setActivePage)
  const toggleSidebar = useWorkbenchStore((s) => s.toggleSidebar)
  const setSettingsOpen = useWorkbenchStore((s) => s.setSettingsOpen)
  const setNotificationsOpen = useWorkbenchStore((s) => s.setNotificationsOpen)
  const savedWorkspaces = useWorkbenchStore((s) => s.savedWorkspaces)
  const addWorkspace = useWorkbenchStore((s) => s.addWorkspace)
  const selectedWorkspaceDir = useWorkbenchStore((s) => s.selectedWorkspaceDir)
  const setSelectedWorkspace = useWorkbenchStore((s) => s.setSelectedWorkspace)
  const searchOpen = useWorkbenchStore((s) => s.searchOpen)
  const setSearchOpen = useWorkbenchStore((s) => s.setSearchOpen)
  const searchQuery = useWorkbenchStore((s) => s.searchQuery)
  const setSearchQuery = useWorkbenchStore((s) => s.setSearchQuery)

  const threads = useSessionStore((s) => s.threads)
  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  const activeWorkspaceDir = useSessionStore((s) => s.activeWorkspaceDir)
  const activeWorkflowId = useSessionStore((s) => s.activeWorkflowId)
  const openThread = useSessionStore((s) => s.openThread)
  const startNewTask = useSessionStore((s) => s.startNewTask)

  const connection = useRuntimeStore((s) => s.connection)

  const [projectsExpanded, setProjectsExpanded] = useState(true)
  const [recentExpanded, setRecentExpanded] = useState(true)
  const [collapsedDirs, setCollapsedDirs] = useState<Record<string, boolean>>({})

  const isMac = window.careDesktop?.platform === 'darwin'

  const ctx: WorkbenchContext = useMemo(
    () => ({
      mode: 'local',
      activeThreadId,
      activeWorkspaceDir,
      activeWorkflowId
    }),
    [activeThreadId, activeWorkspaceDir, activeWorkflowId]
  )

  const primaryPages = useMemo(() => getVisiblePages(ctx, 'sidebar-primary'), [ctx])

  const filteredThreads = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return threads
    return threads.filter(
      (t) =>
        t.title.toLowerCase().includes(q) ||
        t.workspace_dir.toLowerCase().includes(q) ||
        t.workflow_id.toLowerCase().includes(q)
    )
  }, [threads, searchQuery])

  const groupedByWorkspace = useMemo(() => {
    const map = new Map<string, ThreadSummary[]>()
    for (const dir of savedWorkspaces) {
      map.set(dir, [])
    }
    for (const t of filteredThreads) {
      if (!t.workspace_dir) continue
      const existing = map.get(t.workspace_dir) ?? []
      existing.push(t)
      map.set(t.workspace_dir, existing)
    }
    return Array.from(map.entries())
  }, [savedWorkspaces, filteredThreads])

  const attentionCount = useMemo(
    () => threads.filter((t) => t.status === 'interrupted' || t.status === 'crashed').length,
    [threads]
  )

  const handlePickProject = async (): Promise<void> => {
    const dir = await window.careDesktop.pickWorkspaceDirectory()
    if (dir) {
      addWorkspace(dir)
    }
  }

  const toggleProjectDir = (dir: string): void => {
    setCollapsedDirs((prev) => ({ ...prev, [dir]: !prev[dir] }))
  }

  const statusColor =
    connection?.status === 'ready' || connection?.status === 'attached'
      ? 'bg-[var(--status-success)]'
      : connection?.status === 'error'
        ? 'bg-[var(--status-danger)]'
        : 'bg-[var(--status-warning)]'

  return (
    <aside className="flex h-full w-[252px] shrink-0 flex-col border-r border-[var(--border-hairline)] bg-[var(--bg-sidebar)] select-none">
      {/* Top utility row matching Screenshot 3 (traffic lights clearance on macOS) */}
      <div
        className={cn(
          'app-drag flex h-11 shrink-0 items-center justify-between px-3',
          isMac ? 'pl-[76px]' : 'pl-3'
        )}
      >
        <div className="app-no-drag flex items-center gap-1">
          <button
            type="button"
            onClick={toggleSidebar}
            title="收起侧边栏"
            className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
          >
            <PanelLeft className="h-4 w-4" strokeWidth={1.6} />
          </button>
        </div>

        <div className="app-no-drag flex items-center gap-1">
          <button
            type="button"
            onClick={() => setSearchOpen(!searchOpen)}
            title="搜索会话"
            className={cn(
              'inline-flex h-7 w-7 items-center justify-center rounded-lg transition-colors',
              searchOpen
                ? 'bg-[var(--bg-active)] text-[var(--text-primary)]'
                : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]'
            )}
          >
            <Search className="h-3.5 w-3.5" strokeWidth={1.7} />
          </button>
          <button
            type="button"
            onClick={() => setNotificationsOpen(true)}
            title="待办与提醒"
            className="relative inline-flex h-7 w-7 items-center justify-center rounded-lg text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
          >
            <Bell className="h-3.5 w-3.5" strokeWidth={1.7} />
            {attentionCount > 0 && (
              <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-[var(--status-warning)]" />
            )}
          </button>
        </div>
      </div>

      {/* Inline search input when active */}
      {searchOpen && (
        <div className="px-3 pb-2">
          <div className="flex items-center gap-1.5 rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1">
            <Search className="h-3.5 w-3.5 text-[var(--text-muted)]" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索任务或项目..."
              autoFocus
              className="w-full bg-transparent text-ui text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery('')}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        </div>
      )}

      {/* Top 3 pluggable primary navigation buttons (Screenshot 3: 新建任务 / Skill / MCP) */}
      <nav className="space-y-0.5 px-2.5 pb-3 pt-1">
        {primaryPages.map((page) => {
          const Icon = page.icon
          const isSelected =
            page.id === 'chat'
              ? activePageId === 'chat' && activeThreadId === null
              : activePageId === page.id
          return (
            <button
              key={page.id}
              type="button"
              onClick={() => {
                if (page.onSelect) {
                  page.onSelect()
                } else {
                  setActivePage(page.id)
                }
              }}
              className={cn(
                'flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-ui font-medium transition-colors',
                isSelected
                  ? 'bg-[var(--bg-active)] text-[var(--text-primary)]'
                  : 'text-[var(--text-primary)]/90 hover:bg-[var(--bg-hover)]'
              )}
            >
              <Icon
                className="h-[17px] w-[17px] shrink-0 text-[var(--text-primary)]/80"
                strokeWidth={1.65}
              />
              <span className="truncate">{page.title}</span>
            </button>
          )
        })}
      </nav>

      {/* Scrollable Projects & Recent sessions list */}
      <div className="flex-1 overflow-y-auto px-2.5 pb-3">
        {/* Projects section */}
        <Collapsible.Root open={projectsExpanded} onOpenChange={setProjectsExpanded}>
          <div className="flex items-center justify-between px-2 py-1.5">
            <Collapsible.Trigger className="inline-flex items-center gap-1 text-caption font-medium text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
              <span>项目</span>
              <ChevronDown
                className={cn('h-3 w-3 transition-transform', !projectsExpanded && '-rotate-90')}
              />
            </Collapsible.Trigger>
            <button
              type="button"
              onClick={() => void handlePickProject()}
              title="添加本地项目目录"
              className="rounded-md p-0.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            >
              <FolderPlus className="h-3.5 w-3.5" strokeWidth={1.7} />
            </button>
          </div>

          <Collapsible.Content className="space-y-1">
            {groupedByWorkspace.length === 0 ? (
              <button
                type="button"
                onClick={() => void handlePickProject()}
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-ui text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
              >
                <FolderPlus className="h-4 w-4" />
                <span>添加第一个项目目录...</span>
              </button>
            ) : (
              groupedByWorkspace.map(([dir, dirThreads]) => {
                const isCollapsed = Boolean(collapsedDirs[dir])
                const isSelectedDir = selectedWorkspaceDir === dir && activeThreadId === null
                return (
                  <div key={dir}>
                    <div
                      className={cn(
                        'group flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-ui transition-colors',
                        isSelectedDir
                          ? 'bg-[var(--bg-hover)] font-medium text-[var(--text-primary)]'
                          : 'text-[var(--text-primary)]/90 hover:bg-[var(--bg-hover)]'
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => toggleProjectDir(dir)}
                        className="inline-flex h-4 w-4 shrink-0 items-center justify-center text-[var(--text-muted)]"
                      >
                        {isCollapsed ? (
                          <ChevronRight className="h-3 w-3" />
                        ) : (
                          <ChevronDown className="h-3 w-3" />
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setSelectedWorkspace(dir)
                          startNewTask()
                        }}
                        title={dir}
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                      >
                        <Folder
                          className="h-3.5 w-3.5 shrink-0 text-[var(--text-secondary)]"
                          strokeWidth={1.65}
                        />
                        <span className="truncate">{baseName(dir)}</span>
                      </button>
                    </div>

                    {!isCollapsed && dirThreads.length > 0 && (
                      <div className="mt-0.5 space-y-0.5 pl-4">
                        {dirThreads.map((t) => (
                          <ThreadRow
                            key={t.thread_id}
                            thread={t}
                            active={activePageId === 'chat' && activeThreadId === t.thread_id}
                            onSelect={() => void openThread(t)}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </Collapsible.Content>
        </Collapsible.Root>

        {/* Recent section */}
        {filteredThreads.length > 0 && (
          <Collapsible.Root open={recentExpanded} onOpenChange={setRecentExpanded} className="mt-4">
            <div className="flex items-center px-2 py-1.5">
              <Collapsible.Trigger className="inline-flex items-center gap-1 text-caption font-medium text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                <span>最近</span>
                <ChevronDown
                  className={cn('h-3 w-3 transition-transform', !recentExpanded && '-rotate-90')}
                />
              </Collapsible.Trigger>
            </div>
            <Collapsible.Content className="space-y-0.5">
              {filteredThreads.slice(0, 8).map((t) => (
                <ThreadRow
                  key={`recent-${t.thread_id}`}
                  thread={t}
                  active={activePageId === 'chat' && activeThreadId === t.thread_id}
                  onSelect={() => void openThread(t)}
                />
              ))}
            </Collapsible.Content>
          </Collapsible.Root>
        )}
      </div>

      {/* Bottom Space & Settings card (Screenshot 3 bottom-left) */}
      <div className="border-t border-[var(--border-hairline)] p-2.5">
        <button
          type="button"
          onClick={() => setSettingsOpen(true)}
          className="flex w-full items-center justify-between rounded-xl px-2 py-1.5 text-left transition-colors hover:bg-[var(--bg-hover)]"
        >
          <div className="flex min-w-0 items-center gap-2.5">
            <div className="relative flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[var(--accent-primary)] text-caption font-semibold text-[var(--accent-primary-fg)]">
              C
              <span
                className={cn(
                  'absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full border-2 border-[var(--bg-sidebar)]',
                  statusColor
                )}
              />
            </div>
            <div className="min-w-0">
              <div className="truncate text-ui font-medium text-[var(--text-primary)]">
                CARE 工作台
              </div>
              <div className="truncate text-caption text-[var(--text-muted)]">
                {connection?.status === 'ready' || connection?.status === 'attached'
                  ? '个人空间 · 本地就绪'
                  : connection?.status === 'error'
                    ? '本地服务异常'
                    : '正在连接服务...'}
              </div>
            </div>
          </div>
          <Settings className="h-4 w-4 shrink-0 text-[var(--text-muted)]" strokeWidth={1.65} />
        </button>
      </div>
    </aside>
  )
}

function ThreadRow({
  thread,
  active,
  onSelect
}: {
  thread: ThreadSummary
  active: boolean
  onSelect: () => void
}): React.JSX.Element {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        'flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-ui transition-colors',
        active
          ? 'bg-[var(--bg-active)] font-medium text-[var(--text-primary)]'
          : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]'
      )}
    >
      <span className="truncate">{thread.title}</span>
      {thread.status === 'running' && (
        <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--text-muted)]" />
      )}
      {thread.status === 'interrupted' && (
        <span
          title="等待你的确认或输入"
          className="h-2 w-2 shrink-0 rounded-full bg-[var(--status-warning)]"
        />
      )}
      {(thread.status === 'crashed' || thread.status === 'failed') && (
        <AlertCircle className="h-3.5 w-3.5 shrink-0 text-[var(--status-danger)]" />
      )}
    </button>
  )
}
