import { FolderSearch, Sparkles } from 'lucide-react'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'

export function SkillsPage(): React.JSX.Element {
  const selectedWorkspaceDir = useWorkbenchStore((s) => s.selectedWorkspaceDir)

  return (
    <div className="flex-1 overflow-y-auto px-8 py-7">
      <div className="mx-auto max-w-[820px] space-y-6">
        <div>
          <h1 className="text-lg font-semibold text-[var(--text-primary)]">Skill 管理</h1>
          <p className="mt-1 text-xs text-[var(--text-secondary)]">
            按开放 `SKILL.md` 规范发现并管理个人与工作区技能。运行时将在 Phase 1.5 通过 `GET
            /skills` 与 `/runs` 扩展契约接入自动发现与单次会话勾选。
          </p>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-4">
            <div className="flex items-center gap-2 text-[13px] font-medium text-[var(--text-primary)]">
              <Sparkles className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.7} />
              <span>全局个人技能目录</span>
            </div>
            <div className="mt-2 rounded-lg bg-[var(--bg-subtle)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-secondary)]">
              ~/.care/skills/
            </div>
            <p className="mt-2 text-xs text-[var(--text-muted)]">
              放置跨项目复用的个人工作流规范（每个子目录包含一份 `SKILL.md`）。
            </p>
          </div>

          <div className="rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-4">
            <div className="flex items-center gap-2 text-[13px] font-medium text-[var(--text-primary)]">
              <FolderSearch className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.7} />
              <span>当前工作区技能目录</span>
            </div>
            <div className="mt-2 truncate rounded-lg bg-[var(--bg-subtle)] px-2.5 py-1.5 font-mono text-xs text-[var(--text-secondary)]">
              {selectedWorkspaceDir
                ? `${selectedWorkspaceDir}/.agents/skills/`
                : '<未选择工作区>/.agents/skills/'}
            </div>
            <p className="mt-2 text-xs text-[var(--text-muted)]">
              与团队代码仓一同版本化管理，当前默认由 Agent 底座自动发现。
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
