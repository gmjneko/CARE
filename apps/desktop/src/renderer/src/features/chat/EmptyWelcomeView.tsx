import { useEffect, useState } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { ChevronDown, Folder, FolderPlus, GitBranch, Laptop } from 'lucide-react'
import { useRuntimeStore } from '@renderer/stores/runtime-store'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { ComposerCard } from './ComposerCard'

function baseName(dirPath: string): string {
  const parts = dirPath.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || dirPath
}

/**
 * Clean centered welcome page with flush double-layer composer tray and live workspace strip.
 */
export function EmptyWelcomeView(): React.JSX.Element {
  const [draft, setDraft] = useState('')
  const [branchState, setBranchState] = useState<{ dir: string | null; branch: string | null }>({
    dir: null,
    branch: null
  })

  const savedWorkspaces = useWorkbenchStore((s) => s.savedWorkspaces)
  const selectedWorkspaceDir = useWorkbenchStore((s) => s.selectedWorkspaceDir)
  const addWorkspace = useWorkbenchStore((s) => s.addWorkspace)
  const setSelectedWorkspace = useWorkbenchStore((s) => s.setSelectedWorkspace)

  const runtimeError = useRuntimeStore((s) => s.error)
  const restartSidecar = useRuntimeStore((s) => s.restartSidecar)
  const sessionError = useSessionStore((s) => s.error)
  const clearError = useSessionStore((s) => s.clearError)

  useEffect(() => {
    if (!selectedWorkspaceDir) return
    let cancelled = false
    void window.careDesktop
      .getGitSummary(selectedWorkspaceDir)
      .then((res) => {
        if (!cancelled) {
          setBranchState({ dir: selectedWorkspaceDir, branch: res.isGitRepo ? res.branch : null })
        }
      })
      .catch(() => {
        if (!cancelled) setBranchState({ dir: selectedWorkspaceDir, branch: null })
      })
    return () => {
      cancelled = true
    }
  }, [selectedWorkspaceDir])

  const activeBranch =
    selectedWorkspaceDir && branchState.dir === selectedWorkspaceDir ? branchState.branch : null

  const handlePickDir = async (): Promise<void> => {
    const dir = await window.careDesktop.pickWorkspaceDirectory()
    if (dir) {
      addWorkspace(dir)
      clearError()
    }
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center px-6 pb-16">
      <div className="w-full max-w-[760px]">
        {/* Welcome title */}
        <div className="mb-8 text-center">
          <h1 className="care-hero-title inline-block text-display font-semibold tracking-[0.06em]">
            一起探索无限可能
          </h1>
        </div>

        {/* Runtime or validation error banner */}
        {(runtimeError || sessionError) && (
          <div className="mb-3 flex items-center justify-between gap-3 rounded-xl border border-[#f2c6c6] bg-[#fff5f5] px-3.5 py-2 text-ui text-[var(--status-danger)]">
            <span>{sessionError || runtimeError}</span>
            {runtimeError && (
              <button
                type="button"
                onClick={() => void restartSidecar()}
                className="shrink-0 rounded-md border border-[#e8b4b4] bg-white px-2.5 py-0.5 text-caption font-medium hover:bg-[#fff0f0]"
              >
                重试连接
              </button>
            )}
          </div>
        )}

        {/* Flush double-layer tray: white ComposerCard aligned edge-to-edge on top/left/right, soft gray strip below */}
        <div className="care-hero-composer rounded-[20px] bg-[var(--bg-subtle)]/80">
          <ComposerCard value={draft} onValueChange={setDraft} />

          {/* Recessed context strip: 📁 project · 💻 本地 · ⎇ branch */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-ui text-[var(--text-secondary)]">
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild>
                <button
                  type="button"
                  className="inline-flex max-w-[260px] items-center gap-1.5 rounded-lg px-1.5 py-0.5 text-left transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                >
                  <Folder
                    className="h-4 w-4 shrink-0 text-[var(--text-secondary)]"
                    strokeWidth={1.7}
                  />
                  <span className="truncate font-medium text-[var(--text-primary)]/90">
                    {selectedWorkspaceDir ? baseName(selectedWorkspaceDir) : '选择项目目录'}
                  </span>
                  <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
                </button>
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content
                  align="start"
                  sideOffset={6}
                  className="care-menu z-50 min-w-[260px] max-w-[380px] rounded-xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-1.5 shadow-[var(--shadow-floating)]"
                >
                  {savedWorkspaces.map((dir) => (
                    <DropdownMenu.Item
                      key={dir}
                      onSelect={() => {
                        setSelectedWorkspace(dir)
                        clearError()
                      }}
                      className="cursor-pointer rounded-lg px-2.5 py-1.5 outline-none hover:bg-[var(--bg-hover)]"
                    >
                      <div className="truncate text-ui font-medium text-[var(--text-primary)]">
                        {baseName(dir)}
                      </div>
                      <div className="truncate text-caption text-[var(--text-muted)]">{dir}</div>
                    </DropdownMenu.Item>
                  ))}
                  {savedWorkspaces.length > 0 && (
                    <DropdownMenu.Separator className="my-1 h-px bg-[var(--border-hairline)]" />
                  )}
                  <DropdownMenu.Item
                    onSelect={() => void handlePickDir()}
                    className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-ui text-[var(--text-primary)] outline-none hover:bg-[var(--bg-hover)]"
                  >
                    <FolderPlus className="h-4 w-4 text-[var(--text-secondary)]" />
                    <span>浏览并添加本地目录...</span>
                  </DropdownMenu.Item>
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>

            <span className="inline-flex items-center gap-1.5 text-[var(--text-secondary)]">
              <Laptop className="h-4 w-4 text-[var(--text-muted)]" strokeWidth={1.7} />
              <span>本地</span>
            </span>

            {activeBranch && (
              <span className="inline-flex items-center gap-1.5 font-mono text-code text-[var(--text-secondary)]">
                <GitBranch className="h-3.5 w-3.5 text-[var(--text-muted)]" strokeWidth={1.7} />
                <span>{activeBranch}</span>
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
