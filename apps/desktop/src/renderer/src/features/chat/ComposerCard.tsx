import { useEffect, useRef, useState } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import {
  ArrowUp,
  Bot,
  ChevronDown,
  Cpu,
  GitBranch,
  Pause,
  Settings2,
  Sparkles,
  Square,
  Workflow
} from 'lucide-react'
import type { ThinkingLevel, TokenUsage } from '@renderer/sdk/runtime-types'
import { useRuntimeStore } from '@renderer/stores/runtime-store'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { cn } from '@renderer/ui/cn'
import { MenuContent, MenuItem, MenuSeparator } from '@renderer/ui/Menu'

const THINKING_OPTIONS: Array<{ value: ThinkingLevel | null; label: string }> = [
  { value: null, label: '默认推理' },
  { value: 'off', label: '关闭推理' },
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' }
]

interface ComposerCardProps {
  isRunning?: boolean
  isAwaitingInput?: boolean
  latestUsage?: TokenUsage | null
  placeholder?: string
  value?: string
  onValueChange?: (next: string) => void
  className?: string
}

export function ComposerCard({
  isRunning = false,
  isAwaitingInput = false,
  latestUsage = null,
  placeholder,
  value,
  onValueChange,
  className
}: ComposerCardProps): React.JSX.Element {
  const [internalText, setInternalText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const text = value !== undefined ? value : internalText
  const setText = (next: string): void => {
    if (onValueChange) onValueChange(next)
    else setInternalText(next)
  }

  const workflows = useRuntimeStore((s) => s.workflows)
  const settings = useRuntimeStore((s) => s.settings)
  const connection = useRuntimeStore((s) => s.connection)

  const selectedWorkflowId = useWorkbenchStore((s) => s.selectedWorkflowId)
  const setSelectedWorkflow = useWorkbenchStore((s) => s.setSelectedWorkflow)
  const selectedProfile = useWorkbenchStore((s) => s.selectedProfile)
  const setSelectedProfile = useWorkbenchStore((s) => s.setSelectedProfile)
  const selectedThinkingLevel = useWorkbenchStore((s) => s.selectedThinkingLevel)
  const setSelectedThinkingLevel = useWorkbenchStore((s) => s.setSelectedThinkingLevel)
  const setSettingsOpen = useWorkbenchStore((s) => s.setSettingsOpen)

  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  const submitting = useSessionStore((s) => s.submitting)
  const submitPrompt = useSessionStore((s) => s.submitPrompt)
  const sendFollowUp = useSessionStore((s) => s.sendFollowUp)
  const sendSteer = useSessionStore((s) => s.sendSteer)
  const abortActiveRun = useSessionStore((s) => s.abortActiveRun)
  const suspendActiveRun = useSessionStore((s) => s.suspendActiveRun)

  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`
  }, [text])

  const profileNames = Object.keys(settings?.profiles ?? {})
  const effectiveProfileName = selectedProfile ?? settings?.active_profile ?? null
  const effectiveProfileLabel = effectiveProfileName
    ? `${effectiveProfileName}${settings?.profiles[effectiveProfileName]?.model ? ` · ${settings.profiles[effectiveProfileName].model}` : ''}`
    : '默认模型'

  const currentWorkflow = workflows.find((w) => w.name === selectedWorkflowId)
  const workflowLabel =
    selectedWorkflowId === 'plan_and_execute'
      ? 'Plan & Execute'
      : selectedWorkflowId === 'single_agent'
        ? 'Single Agent'
        : (currentWorkflow?.name ?? selectedWorkflowId)

  const runtimeReady = connection?.status === 'ready' || connection?.status === 'attached'

  const effectivePlaceholder =
    placeholder ??
    (isRunning
      ? '执行中：Enter 排队，⌘/Ctrl+Enter 插话'
      : isAwaitingInput
        ? '输入修改意见继续打磨，或点击上方确认按钮进入下一步...'
        : '描述任务，输入 / 调用技能')

  const handleSend = async (mode: 'normal' | 'steer' = 'normal'): Promise<void> => {
    const trimmed = text.trim()
    if (!trimmed) return
    setText('')
    if (isRunning) {
      if (mode === 'steer') {
        await sendSteer(trimmed)
      } else {
        await sendFollowUp(trimmed)
      }
      return
    }
    await submitPrompt(trimmed)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.nativeEvent.isComposing) return
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      void handleSend(isRunning ? 'steer' : 'normal')
      return
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void handleSend('normal')
    }
  }

  const formatTokens = (n: number): string => {
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
    return String(n)
  }

  return (
    <div
      className={cn(
        'rounded-[20px] border border-[var(--border-hairline)] bg-[var(--bg-surface)] shadow-[var(--shadow-composer)] transition-[border-color,box-shadow] focus-within:border-[var(--border-card)]',
        className
      )}
    >
      <div className="px-4 pb-1.5 pt-3.5">
        <textarea
          ref={textareaRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={!runtimeReady || submitting}
          placeholder={runtimeReady ? effectivePlaceholder : '正在连接本地 Agent Runtime...'}
          className={cn(
            'max-h-[180px] min-h-[42px] w-full resize-none bg-transparent text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none disabled:opacity-60',
            activeThreadId ? 'text-ui' : 'text-body'
          )}
        />
      </div>

      {/* Bottom control bar inside floating composer */}
      <div className="flex items-center justify-between gap-2 px-3 pb-3 pt-1">
        <div className="flex min-w-0 items-center gap-1">
          {/* Workflow selector (locked once a thread starts) */}
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild disabled={Boolean(activeThreadId)}>
              <button
                type="button"
                title={activeThreadId ? '已有会话的工作流不可切换' : '选择工作流模式'}
                className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-ui text-[var(--text-primary)]/80 transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:cursor-default disabled:opacity-70"
              >
                <GitBranch className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.7} />
                <span>{workflowLabel}</span>
                {!activeThreadId && (
                  <ChevronDown className="h-3.5 w-3.5 text-[var(--text-muted)]" />
                )}
              </button>
            </DropdownMenu.Trigger>
            <MenuContent align="start" className="min-w-[260px]">
              {(workflows.length > 0
                ? workflows
                : [
                    { name: 'single_agent', description: '通用单 Agent Vibe Coding 对话' },
                    { name: 'plan_and_execute', description: '先交互规划，确认后自动派生执行' }
                  ]
              ).map((wf) => (
                <MenuItem
                  key={wf.name}
                  icon={
                    wf.name === 'plan_and_execute' ? (
                      <Workflow className="h-4 w-4" strokeWidth={1.65} />
                    ) : (
                      <Bot className="h-4 w-4" strokeWidth={1.65} />
                    )
                  }
                  label={
                    wf.name === 'plan_and_execute'
                      ? 'Plan & Execute'
                      : wf.name === 'single_agent'
                        ? 'Single Agent'
                        : wf.name
                  }
                  description={wf.description}
                  selected={selectedWorkflowId === wf.name}
                  onSelect={() => setSelectedWorkflow(wf.name)}
                />
              ))}
            </MenuContent>
          </DropdownMenu.Root>

          {/* Thinking level selector */}
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-ui text-[var(--text-primary)]/80 transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
              >
                <Sparkles className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.65} />
                <span>
                  {THINKING_OPTIONS.find((o) => o.value === selectedThinkingLevel)?.label ??
                    '默认推理'}
                </span>
                <ChevronDown className="h-3.5 w-3.5 text-[var(--text-muted)]" />
              </button>
            </DropdownMenu.Trigger>
            <MenuContent align="start" className="min-w-[144px]">
              {THINKING_OPTIONS.map((opt) => (
                <MenuItem
                  key={String(opt.value)}
                  label={opt.label}
                  selected={selectedThinkingLevel === opt.value}
                  onSelect={() => setSelectedThinkingLevel(opt.value)}
                />
              ))}
            </MenuContent>
          </DropdownMenu.Root>
        </div>

        <div className="flex items-center gap-1.5">
          {/* Compact token usage indicator */}
          {latestUsage && (latestUsage.total_tokens ?? 0) > 0 && (
            <span
              title={`输入 ${latestUsage.input_tokens ?? 0} · 输出 ${latestUsage.output_tokens ?? 0} · 缓存命中 ${latestUsage.cached_input_tokens ?? 0}`}
              className="hidden items-center gap-1 rounded-full bg-[var(--bg-subtle)] px-2.5 py-0.5 text-caption text-[var(--text-muted)] sm:inline-flex"
            >
              {formatTokens(latestUsage.total_tokens ?? 0)} tok
            </span>
          )}

          {/* Model Profile selector */}
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className="inline-flex h-8 max-w-[240px] items-center gap-1.5 rounded-lg px-2.5 text-ui text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-hover)]"
              >
                <Cpu className="h-4 w-4 shrink-0 text-[var(--text-secondary)]" strokeWidth={1.65} />
                <span className="truncate font-medium">{effectiveProfileLabel}</span>
                <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
              </button>
            </DropdownMenu.Trigger>
            <MenuContent align="end" className="min-w-[220px]">
              <MenuItem
                label={`使用默认配置${settings?.active_profile ? `（${settings.active_profile}）` : ''}`}
                selected={selectedProfile === null}
                onSelect={() => setSelectedProfile(null)}
              />
              {profileNames.map((name) => {
                const prof = settings!.profiles[name]
                return (
                  <MenuItem
                    key={name}
                    label={name}
                    description={prof.model}
                    selected={selectedProfile === name}
                    onSelect={() => setSelectedProfile(name)}
                  />
                )
              })}
              <MenuSeparator />
              <MenuItem
                icon={<Settings2 className="h-4 w-4" strokeWidth={1.65} />}
                label="管理模型配置..."
                muted
                onSelect={() => setSettingsOpen(true)}
              />
            </MenuContent>
          </DropdownMenu.Root>

          {/* Running controls: Suspend + Stop (Screenshot 2) OR Idle Send button */}
          {isRunning ? (
            <div className="flex items-center gap-1">
              {text.trim() ? (
                <button
                  type="button"
                  onClick={() => void handleSend('steer')}
                  title="立即插话纠偏 (⌘/Ctrl+Enter)"
                  className="inline-flex h-8 items-center rounded-full bg-[var(--bg-subtle)] px-3 text-ui font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-active)]"
                >
                  插话
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void suspendActiveRun()}
                  title="挂起并保留执行现场"
                  className="inline-flex h-8 w-8 items-center justify-center rounded-full text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                >
                  <Pause className="h-4 w-4" strokeWidth={1.8} />
                </button>
              )}
              <button
                type="button"
                onClick={() => void abortActiveRun()}
                title="中止当前执行"
                className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-[var(--accent-primary)] text-[var(--accent-primary-fg)] transition-all hover:opacity-85 active:scale-95"
              >
                <Square className="h-3 w-3 fill-current" />
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => void handleSend('normal')}
              disabled={!runtimeReady || submitting || !text.trim()}
              title="发送 (Enter)"
              className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-[var(--accent-primary)] text-[var(--accent-primary-fg)] transition-all hover:opacity-85 active:scale-95 disabled:bg-[#d7d7d2] disabled:text-white disabled:active:scale-100"
            >
              <ArrowUp className="h-4 w-4" strokeWidth={2} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
