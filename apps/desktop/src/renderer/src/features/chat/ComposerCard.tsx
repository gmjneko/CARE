import { useEffect, useRef, useState } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { ArrowUp, ChevronDown, Cpu, GitBranch, Pause, Sparkles, Square } from 'lucide-react'
import type { ThinkingLevel, TokenUsage } from '@renderer/sdk/runtime-types'
import { useRuntimeStore } from '@renderer/stores/runtime-store'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'
import { cn } from '@renderer/ui/cn'

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
        'rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] shadow-[var(--shadow-composer)] transition-shadow focus-within:border-[#d5d5ce]',
        className
      )}
    >
      <div className="px-4 pt-3.5 pb-2">
        <textarea
          ref={textareaRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={!runtimeReady || submitting}
          placeholder={runtimeReady ? effectivePlaceholder : '正在连接本地 Agent Runtime...'}
          className="max-h-[180px] min-h-[28px] w-full resize-none bg-transparent text-[14px] leading-relaxed text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none disabled:opacity-60"
        />
      </div>

      {/* Bottom control bar inside floating composer */}
      <div className="flex items-center justify-between gap-2 px-3 pb-2.5 pt-1">
        <div className="flex min-w-0 items-center gap-1">
          {/* Workflow selector (locked once a thread starts) */}
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild disabled={Boolean(activeThreadId)}>
              <button
                type="button"
                title={activeThreadId ? '已有会话的工作流不可切换' : '选择工作流模式'}
                className="inline-flex h-7 items-center gap-1.5 rounded-lg px-2 text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:cursor-default disabled:opacity-70"
              >
                <GitBranch className="h-3.5 w-3.5" strokeWidth={1.7} />
                <span>{workflowLabel}</span>
                {!activeThreadId && <ChevronDown className="h-3 w-3 text-[var(--text-muted)]" />}
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                align="start"
                sideOffset={6}
                className="z-50 min-w-[220px] rounded-xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-1 shadow-[var(--shadow-floating)]"
              >
                {(workflows.length > 0
                  ? workflows
                  : [
                      { name: 'single_agent', description: '通用单 Agent Vibe Coding 对话' },
                      { name: 'plan_and_execute', description: '先交互规划，确认后自动派生执行' }
                    ]
                ).map((wf) => (
                  <DropdownMenu.Item
                    key={wf.name}
                    onSelect={() => setSelectedWorkflow(wf.name)}
                    className={cn(
                      'cursor-pointer rounded-lg px-2.5 py-2 outline-none hover:bg-[var(--bg-hover)]',
                      selectedWorkflowId === wf.name && 'bg-[var(--bg-subtle)]'
                    )}
                  >
                    <div className="text-xs font-medium text-[var(--text-primary)]">{wf.name}</div>
                    <div className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                      {wf.description}
                    </div>
                  </DropdownMenu.Item>
                ))}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>

          {/* Thinking level selector */}
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className="inline-flex h-7 items-center gap-1 rounded-lg px-2 text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
              >
                <Sparkles className="h-3.5 w-3.5" strokeWidth={1.65} />
                <span>
                  {THINKING_OPTIONS.find((o) => o.value === selectedThinkingLevel)?.label ??
                    '默认推理'}
                </span>
                <ChevronDown className="h-3 w-3 text-[var(--text-muted)]" />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                align="start"
                sideOffset={6}
                className="z-50 min-w-[130px] rounded-xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-1 shadow-[var(--shadow-floating)]"
              >
                {THINKING_OPTIONS.map((opt) => (
                  <DropdownMenu.Item
                    key={String(opt.value)}
                    onSelect={() => setSelectedThinkingLevel(opt.value)}
                    className={cn(
                      'cursor-pointer rounded-lg px-2.5 py-1.5 text-xs text-[var(--text-primary)] outline-none hover:bg-[var(--bg-hover)]',
                      selectedThinkingLevel === opt.value && 'bg-[var(--bg-subtle)] font-medium'
                    )}
                  >
                    {opt.label}
                  </DropdownMenu.Item>
                ))}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        </div>

        <div className="flex items-center gap-1.5">
          {/* Compact token usage indicator */}
          {latestUsage && (latestUsage.total_tokens ?? 0) > 0 && (
            <span
              title={`输入 ${latestUsage.input_tokens ?? 0} · 输出 ${latestUsage.output_tokens ?? 0} · 缓存命中 ${latestUsage.cached_input_tokens ?? 0}`}
              className="hidden items-center gap-1 rounded-full bg-[var(--bg-subtle)] px-2 py-0.5 text-[11px] text-[var(--text-muted)] sm:inline-flex"
            >
              {formatTokens(latestUsage.total_tokens ?? 0)} tok
            </span>
          )}

          {/* Model Profile selector */}
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className="inline-flex h-7 max-w-[190px] items-center gap-1 rounded-lg px-2 text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
              >
                <Cpu className="h-3.5 w-3.5 shrink-0" strokeWidth={1.65} />
                <span className="truncate">{effectiveProfileLabel}</span>
                <ChevronDown className="h-3 w-3 shrink-0 text-[var(--text-muted)]" />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                align="end"
                sideOffset={6}
                className="z-50 min-w-[200px] rounded-xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-1 shadow-[var(--shadow-floating)]"
              >
                <DropdownMenu.Item
                  onSelect={() => setSelectedProfile(null)}
                  className={cn(
                    'cursor-pointer rounded-lg px-2.5 py-1.5 text-xs text-[var(--text-primary)] outline-none hover:bg-[var(--bg-hover)]',
                    selectedProfile === null && 'bg-[var(--bg-subtle)] font-medium'
                  )}
                >
                  使用默认配置
                  {settings?.active_profile ? `（${settings.active_profile}）` : ''}
                </DropdownMenu.Item>
                {profileNames.map((name) => {
                  const prof = settings!.profiles[name]
                  return (
                    <DropdownMenu.Item
                      key={name}
                      onSelect={() => setSelectedProfile(name)}
                      className={cn(
                        'cursor-pointer rounded-lg px-2.5 py-1.5 text-xs outline-none hover:bg-[var(--bg-hover)]',
                        selectedProfile === name && 'bg-[var(--bg-subtle)] font-medium'
                      )}
                    >
                      <div className="text-[var(--text-primary)]">{name}</div>
                      <div className="text-[11px] text-[var(--text-muted)]">{prof.model}</div>
                    </DropdownMenu.Item>
                  )
                })}
                <DropdownMenu.Separator className="my-1 h-px bg-[var(--border-hairline)]" />
                <DropdownMenu.Item
                  onSelect={() => setSettingsOpen(true)}
                  className="cursor-pointer rounded-lg px-2.5 py-1.5 text-xs text-[var(--text-secondary)] outline-none hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                >
                  管理模型配置...
                </DropdownMenu.Item>
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>

          {/* Running controls: Suspend + Stop (Screenshot 2) OR Idle Send button */}
          {isRunning ? (
            <div className="flex items-center gap-1">
              {text.trim() ? (
                <button
                  type="button"
                  onClick={() => void handleSend('steer')}
                  title="立即插话纠偏 (⌘/Ctrl+Enter)"
                  className="inline-flex h-7 items-center rounded-full bg-[var(--bg-subtle)] px-2.5 text-[11.5px] font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-active)]"
                >
                  插话
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void suspendActiveRun()}
                  title="挂起并保留执行现场"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-full text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                >
                  <Pause className="h-3.5 w-3.5" strokeWidth={1.8} />
                </button>
              )}
              <button
                type="button"
                onClick={() => void abortActiveRun()}
                title="中止当前执行"
                className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-[var(--accent-primary)] text-[var(--accent-primary-fg)] transition-opacity hover:opacity-85"
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
              className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-[var(--accent-primary)] text-[var(--accent-primary-fg)] transition-opacity hover:opacity-85 disabled:bg-[#d7d7d2] disabled:text-white"
            >
              <ArrowUp className="h-4 w-4" strokeWidth={2} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
