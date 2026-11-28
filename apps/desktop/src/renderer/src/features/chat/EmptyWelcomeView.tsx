import { useState } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { ChevronDown, Folder, FolderPlus } from 'lucide-react'
import { useRuntimeStore } from '@renderer/stores/runtime-store'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { ComposerCard } from './ComposerCard'

const QUICK_PROMPTS = [
  { label: '梳理当前仓库架构', prompt: '介绍一下当前这个项目的目录结构、核心模块和整体架构。' },
  { label: '规划并实现新功能', prompt: '先帮我分析需求并给出实现计划，确认后再开始修改代码。' }
]

function baseName(dirPath: string): string {
  const parts = dirPath.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || dirPath
}

/**
 * Clean centered welcome page aligned with Screenshot 3.
 */
export function EmptyWelcomeView(): React.JSX.Element {
  const [draft, setDraft] = useState('')

  const savedWorkspaces = useWorkbenchStore((s) => s.savedWorkspaces)
  const selectedWorkspaceDir = useWorkbenchStore((s) => s.selectedWorkspaceDir)
  const addWorkspace = useWorkbenchStore((s) => s.addWorkspace)
  const setSelectedWorkspace = useWorkbenchStore((s) => s.setSelectedWorkspace)
  const setSelectedWorkflow = useWorkbenchStore((s) => s.setSelectedWorkflow)

  const runtimeError = useRuntimeStore((s) => s.error)
  const restartSidecar = useRuntimeStore((s) => s.restartSidecar)
  const sessionError = useSessionStore((s) => s.error)
  const clearError = useSessionStore((s) => s.clearError)

  const handlePickDir = async (): Promise<void> => {
    const dir = await window.careDesktop.pickWorkspaceDirectory()
    if (dir) {
      addWorkspace(dir)
      clearError()
    }
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center px-6 pb-14">
      <div className="w-full max-w-[660px]">
        {/* Minimal brand emblem + calm headline (Screenshot 3) */}
        <div className="mb-7 flex flex-col items-center text-center">
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] text-heading font-semibold tracking-tight text-[var(--text-primary)] shadow-[var(--shadow-floating)]">
            CARE
          </div>
          <h1 className="text-display font-semibold tracking-tight text-[var(--text-primary)]">
            和 CARE 一起探索无限可能
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

        {/* Centered floating ComposerCard */}
        <ComposerCard value={draft} onValueChange={setDraft} />

        {/* Attached recessed project selector pill right beneath composer (Screenshot 3) */}
        <div className="mx-3 -mt-1 flex items-center justify-between rounded-b-2xl border border-t-0 border-[var(--border-hairline)] bg-[var(--bg-subtle)]/80 px-3.5 py-2.5 text-ui text-[var(--text-secondary)]">
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className="inline-flex max-w-[420px] items-center gap-1.5 rounded-md px-1.5 py-0.5 text-left transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
              >
                <Folder className="h-4 w-4 shrink-0 text-[var(--text-muted)]" strokeWidth={1.7} />
                <span className="truncate font-medium">
                  {selectedWorkspaceDir ? baseName(selectedWorkspaceDir) : '选择本地项目目录'}
                </span>
                <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                align="start"
                sideOffset={6}
                className="z-50 min-w-[260px] max-w-[380px] rounded-xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-1.5 shadow-[var(--shadow-floating)]"
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

          {selectedWorkspaceDir && (
            <span className="hidden truncate pl-3 text-caption text-[var(--text-muted)] sm:inline">
              {selectedWorkspaceDir}
            </span>
          )}
        </div>

        {/* Subtle suggestion links underneath (Screenshot 3) */}
        <div className="mt-5 flex flex-wrap items-center justify-center gap-5 text-ui text-[var(--text-muted)]">
          {QUICK_PROMPTS.map((item, idx) => (
            <button
              key={item.label}
              type="button"
              onClick={() => {
                if (idx === 1) setSelectedWorkflow('plan_and_execute')
                setDraft(item.prompt)
              }}
              className="transition-colors hover:text-[var(--text-primary)]"
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
