import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  CheckCircle2,
  ChevronRight,
  FileCode2,
  FileDiff,
  Folder,
  GitBranch,
  Laptop,
  Loader2,
  Package,
  RefreshCw
} from 'lucide-react'
import type { GitWorkspaceSummary, WorkspaceFileEntry } from '@shared/ipc-contracts'
import { projectEventsToTimeline } from '@renderer/sdk/event-projection'
import type { InspectorWidgetProps } from '@renderer/shell/contracts'
import { useSessionStore } from '@renderer/stores/session-store'
import { cn } from '@renderer/ui/cn'

function DiffDetailView({
  workspaceDir,
  summary
}: {
  workspaceDir: string
  summary: GitWorkspaceSummary
}): React.JSX.Element {
  const [selectedPath, setSelectedPath] = useState<string | null>(summary.files[0]?.path ?? null)
  const [diffState, setDiffState] = useState<{ path: string | null; text: string }>({
    path: null,
    text: ''
  })

  useEffect(() => {
    if (!selectedPath) return
    let cancelled = false
    void window.careDesktop
      .getGitFileDiff(workspaceDir, selectedPath)
      .then((text) => {
        if (!cancelled) setDiffState({ path: selectedPath, text })
      })
      .catch((err) => {
        if (!cancelled) setDiffState({ path: selectedPath, text: `无法读取 Diff: ${String(err)}` })
      })
    return () => {
      cancelled = true
    }
  }, [workspaceDir, selectedPath])

  if (summary.files.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-ui text-[var(--text-muted)]">
        当前工作区没有未提交的变更
      </div>
    )
  }

  const loading = selectedPath !== null && diffState.path !== selectedPath

  return (
    <div className="flex h-full flex-col">
      <div className="max-h-[168px] shrink-0 overflow-y-auto border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)]/40 p-2">
        <div className="space-y-0.5">
          {summary.files.map((file) => (
            <button
              key={file.path}
              type="button"
              onClick={() => setSelectedPath(file.path)}
              className={cn(
                'flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-ui transition-colors',
                selectedPath === file.path
                  ? 'bg-[var(--bg-active)] font-medium text-[var(--text-primary)]'
                  : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]'
              )}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className="w-4 shrink-0 font-mono text-caption text-[var(--text-muted)]">
                  {file.status}
                </span>
                <span className="truncate font-mono text-code">{file.path}</span>
              </span>
              <span className="flex shrink-0 items-center gap-1.5 font-mono text-caption">
                {file.additions > 0 && (
                  <span className="text-[var(--status-success)]">+{file.additions}</span>
                )}
                {file.deletions > 0 && (
                  <span className="text-[var(--status-danger)]">-{file.deletions}</span>
                )}
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-auto bg-[var(--bg-surface)] p-3 font-mono text-code">
        {loading ? (
          <div className="flex items-center gap-2 text-ui text-[var(--text-muted)]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>正在加载变更内容...</span>
          </div>
        ) : (
          diffState.text.split('\n').map((line, i) => {
            const isAdd = line.startsWith('+') && !line.startsWith('+++')
            const isDel = line.startsWith('-') && !line.startsWith('---')
            const isHunk = line.startsWith('@@')
            return (
              <div
                key={i}
                className={cn(
                  'whitespace-pre px-2',
                  isAdd && 'bg-[#edf9f0] text-[#1e6638]',
                  isDel && 'bg-[#fdf0f0] text-[#9e2626]',
                  isHunk && 'bg-[var(--bg-subtle)] text-[var(--text-muted)]'
                )}
              >
                {line || ' '}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

export function EnvironmentWidget({
  workspaceDir,
  openDetail
}: InspectorWidgetProps): React.JSX.Element {
  const [summaryState, setSummaryState] = useState<{
    dir: string | null
    data: GitWorkspaceSummary | null
  }>({ dir: null, data: null })
  const [refreshing, setRefreshing] = useState(false)
  const eventCount = useSessionStore((s) => s.events.length)

  const fetchSummary = useCallback((dir: string) => {
    let cancelled = false
    void window.careDesktop
      .getGitSummary(dir)
      .then((res) => {
        if (!cancelled) setSummaryState({ dir, data: res })
      })
      .catch(() => {
        if (!cancelled) setSummaryState({ dir, data: null })
      })
      .finally(() => {
        if (!cancelled) setRefreshing(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!workspaceDir) return
    return fetchSummary(workspaceDir)
  }, [workspaceDir, eventCount, fetchSummary])

  const summary = workspaceDir && summaryState.dir === workspaceDir ? summaryState.data : null

  const handleManualRefresh = (): void => {
    if (!workspaceDir) return
    setRefreshing(true)
    fetchSummary(workspaceDir)
  }

  const handleOpenDiff = (): void => {
    if (!workspaceDir || !summary) return
    openDetail({
      id: 'git-diff',
      title: '工作区变更',
      subtitle: `${summary.files.length} 个文件 · +${summary.totalAdditions} -${summary.totalDeletions}`,
      render: () => <DiffDetailView workspaceDir={workspaceDir} summary={summary} />
    })
  }

  return (
    <div className="space-y-1.5 text-ui text-[var(--text-secondary)]">
      <button
        type="button"
        disabled={!summary?.isGitRepo}
        onClick={handleOpenDiff}
        className="flex w-full items-center justify-between rounded-lg px-1.5 py-1 transition-colors hover:bg-[var(--bg-hover)] disabled:cursor-default disabled:hover:bg-transparent"
      >
        <span className="inline-flex items-center gap-2 text-[var(--text-primary)]">
          <FileDiff className="h-4 w-4 text-[var(--text-muted)]" strokeWidth={1.7} />
          <span>变更</span>
        </span>
        <span className="inline-flex items-center gap-1.5 font-mono text-caption">
          {summary?.isGitRepo ? (
            summary.files.length > 0 ? (
              <>
                <span className="text-[var(--status-success)]">+{summary.totalAdditions}</span>
                <span className="text-[var(--status-danger)]">-{summary.totalDeletions}</span>
                <ChevronRight className="h-3.5 w-3.5 text-[var(--text-muted)]" />
              </>
            ) : (
              <span className="text-[var(--text-muted)]">无变更</span>
            )
          ) : (
            <span className="text-[var(--text-muted)]">非 Git 仓库</span>
          )}
        </span>
      </button>

      <div className="flex items-center justify-between px-1.5 py-1">
        <span className="inline-flex items-center gap-2 text-[var(--text-primary)]">
          <Laptop className="h-4 w-4 text-[var(--text-muted)]" strokeWidth={1.7} />
          <span>本地</span>
        </span>
        <span className="text-caption text-[var(--text-muted)]">Sidecar</span>
      </div>

      {summary?.isGitRepo && (
        <div className="flex items-center justify-between px-1.5 py-1">
          <span className="inline-flex min-w-0 items-center gap-2 text-[var(--text-primary)]">
            <GitBranch className="h-4 w-4 shrink-0 text-[var(--text-muted)]" strokeWidth={1.7} />
            <span className="truncate font-mono text-code">{summary.branch || 'HEAD'}</span>
          </span>
          <button
            type="button"
            onClick={handleManualRefresh}
            title="刷新 Git 状态"
            className="rounded p-0.5 text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', refreshing && 'animate-spin')} />
          </button>
        </div>
      )}
    </div>
  )
}

export function TaskListWidget({ workflowId }: InspectorWidgetProps): React.JSX.Element {
  const events = useSessionStore((s) => s.events)
  const timeline = useMemo(() => projectEventsToTimeline(events), [events])

  // Single-agent chats have no multi-step plan/todo list yet; show nodes only for multi-stage plans.
  const isPlanWorkflow =
    (workflowId !== null && workflowId !== 'single_agent') || timeline.nodes.length > 1
  if (!isPlanWorkflow || timeline.nodes.length === 0) {
    return <div className="px-1.5 py-1 text-ui text-[var(--text-muted)]">暂无任务</div>
  }

  return (
    <div className="space-y-1.5">
      {timeline.nodes.map((node) => (
        <div key={node.nodeId} className="rounded-lg bg-[var(--bg-subtle)]/60 px-2.5 py-2 text-ui">
          <div className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 font-medium text-[var(--text-primary)]">
              {node.status === 'running' ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--text-muted)]" />
              ) : (
                <CheckCircle2
                  className={cn(
                    'h-3.5 w-3.5',
                    node.status === 'completed'
                      ? 'text-[var(--status-success)]'
                      : 'text-[var(--status-warning)]'
                  )}
                />
              )}
              <span>{node.nodeId}</span>
            </span>
            <span className="text-caption text-[var(--text-muted)]">{node.status}</span>
          </div>
          {node.summary && (
            <p className="mt-1 line-clamp-2 text-caption text-[var(--text-secondary)]">
              {node.summary}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

export function ArtifactsWidget(): React.JSX.Element {
  return (
    <div className="flex items-center gap-2 px-1.5 py-1 text-ui text-[var(--text-muted)]">
      <Package className="h-4 w-4" strokeWidth={1.6} />
      <span>暂无产物</span>
    </div>
  )
}

function FilePreviewDetail({
  workspaceDir,
  relativePath
}: {
  workspaceDir: string
  relativePath: string
}): React.JSX.Element {
  const [fileState, setFileState] = useState<{
    key: string
    content: string
    truncated: boolean
  } | null>(null)
  const currentKey = `${workspaceDir}::${relativePath}`

  useEffect(() => {
    let cancelled = false
    void window.careDesktop
      .readWorkspaceFile(workspaceDir, relativePath)
      .then((res) => {
        if (!cancelled) {
          setFileState({ key: currentKey, content: res.content, truncated: res.truncated })
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setFileState({
            key: currentKey,
            content: `无法读取文件: ${String(err)}`,
            truncated: false
          })
        }
      })
    return () => {
      cancelled = true
    }
  }, [workspaceDir, relativePath, currentKey])

  if (!fileState || fileState.key !== currentKey) {
    return (
      <div className="flex items-center gap-2 p-4 text-ui text-[var(--text-muted)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <span>正在读取文件...</span>
      </div>
    )
  }

  return (
    <div className="p-3">
      {fileState.truncated && (
        <div className="mb-2 rounded-lg bg-[#fffaf0] px-2.5 py-1 text-caption text-[var(--status-warning)]">
          文件超过 256 KiB，已截断预览
        </div>
      )}
      <pre className="overflow-x-auto font-mono text-code text-[var(--text-primary)]">
        <code>{fileState.content}</code>
      </pre>
    </div>
  )
}

export function ReferencesWidget({
  workspaceDir,
  openDetail
}: InspectorWidgetProps): React.JSX.Element {
  const [entriesState, setEntriesState] = useState<{
    dir: string | null
    items: WorkspaceFileEntry[]
  }>({ dir: null, items: [] })
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!workspaceDir) return
    let cancelled = false
    void window.careDesktop
      .listWorkspaceFiles(workspaceDir)
      .then((items) => {
        if (!cancelled) setEntriesState({ dir: workspaceDir, items })
      })
      .catch(() => {
        if (!cancelled) setEntriesState({ dir: workspaceDir, items: [] })
      })
    return () => {
      cancelled = true
    }
  }, [workspaceDir])

  if (!workspaceDir) {
    return <div className="px-1.5 py-1 text-ui text-[var(--text-muted)]">未绑定项目目录</div>
  }

  const entries = entriesState.dir === workspaceDir ? entriesState.items : []

  return (
    <div className="space-y-1 text-ui">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="flex w-full items-center justify-between rounded-lg px-1.5 py-1 text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
      >
        <span className="inline-flex items-center gap-1.5">
          <ChevronRight
            className={cn(
              'h-3.5 w-3.5 text-[var(--text-muted)] transition-transform',
              expanded && 'rotate-90'
            )}
          />
          <span>项目文件</span>
        </span>
        <span className="text-caption text-[var(--text-muted)]">{entries.length}</span>
      </button>

      {expanded && (
        <div className="max-h-[240px] space-y-0.5 overflow-y-auto pl-3">
          {entries.map((entry) => (
            <button
              key={entry.relativePath}
              type="button"
              disabled={entry.isDirectory}
              onClick={() => {
                if (entry.isDirectory) return
                openDetail({
                  id: `file:${entry.relativePath}`,
                  title: entry.name,
                  subtitle: entry.relativePath,
                  render: () => (
                    <FilePreviewDetail
                      workspaceDir={workspaceDir}
                      relativePath={entry.relativePath}
                    />
                  )
                })
              }}
              className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-code text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:cursor-default"
            >
              {entry.isDirectory ? (
                <Folder
                  className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]"
                  strokeWidth={1.6}
                />
              ) : (
                <FileCode2
                  className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]"
                  strokeWidth={1.6}
                />
              )}
              <span className="truncate font-mono">{entry.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
