import { useState } from 'react'
import { AlertTriangle, Check, Play, RotateCcw, ShieldAlert, X } from 'lucide-react'
import type { InteractionRequestEvent, PendingInterrupt } from '@renderer/sdk/runtime-types'
import { useSessionStore } from '@renderer/stores/session-store'

interface InteractionBannerProps {
  liveInteractions: Array<{ nodeId: string | null; request: InteractionRequestEvent }>
  pendingInterrupts: PendingInterrupt[]
  resumable: boolean
}

/**
 * Inline HITL card supporting:
 * - Blocking tool approvals & agent user_input questions (hot & cold)
 * - Interactive node gates (e.g. plan_and_execute planner awaiting_input -> complete=True)
 * - Crash/suspended/failed thread recovery & discard
 */
export function InteractionBanner({
  liveInteractions,
  pendingInterrupts,
  resumable
}: InteractionBannerProps): React.JSX.Element | null {
  const [answerText, setAnswerText] = useState('')
  const [feedback, setFeedback] = useState('')

  const submitting = useSessionStore((s) => s.submitting)
  const completeAwaitingNode = useSessionStore((s) => s.completeAwaitingNode)
  const resumeWithPrompt = useSessionStore((s) => s.resumeWithPrompt)
  const respondInteraction = useSessionStore((s) => s.respondInteraction)
  const discardActiveThread = useSessionStore((s) => s.discardActiveThread)

  // 1. Live blocking interaction (tool approval / ask_question)
  if (liveInteractions.length > 0) {
    const first = liveInteractions[0]
    return (
      <InteractionRequestCard
        request={first.request}
        nodeId={first.nodeId}
        answerText={answerText}
        setAnswerText={setAnswerText}
        feedback={feedback}
        setFeedback={setFeedback}
        submitting={submitting}
        onRespond={(decision, value, fb) => {
          void respondInteraction(
            {
              request_id: first.request.request_id,
              decision,
              value: value ?? null,
              feedback: fb ?? null
            },
            first.nodeId ?? undefined
          )
          setAnswerText('')
          setFeedback('')
        }}
      />
    )
  }

  if (!resumable || pendingInterrupts.length === 0) {
    return null
  }

  const primary = pendingInterrupts[0]

  // 2. Cold paused interaction (approval / user_input)
  if (primary.interaction) {
    return (
      <InteractionRequestCard
        request={primary.interaction}
        nodeId={primary.node_id}
        answerText={answerText}
        setAnswerText={setAnswerText}
        feedback={feedback}
        setFeedback={setFeedback}
        submitting={submitting}
        onRespond={(decision, value, fb) => {
          void respondInteraction(
            {
              request_id: primary.interaction!.request_id,
              decision,
              value: value ?? null,
              feedback: fb ?? null
            },
            primary.node_id
          )
          setAnswerText('')
          setFeedback('')
        }}
      />
    )
  }

  // 3. Interactive node awaiting_input (e.g., plan_and_execute planner / executor)
  if (primary.kind === 'awaiting_input') {
    const isPlanner = primary.node_id === 'planner'
    return (
      <div className="my-4 rounded-2xl border border-[var(--border-card)] bg-[var(--bg-subtle)]/70 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-ui font-semibold text-[var(--text-primary)]">
              {isPlanner ? '规划阶段等待确认' : `节点「${primary.node_id}」等待继续或收口`}
            </div>
            <div className="mt-0.5 text-caption text-[var(--text-secondary)]">
              {isPlanner
                ? '你可以直接在下方输入框提出修改意见继续打磨计划，或确认并进入执行阶段。'
                : '可以继续追问微调，或点击完成结束本轮工作流。'}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={submitting}
              onClick={() => void discardActiveThread()}
              className="inline-flex h-8 items-center gap-1 rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-3 text-ui font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-50"
            >
              放弃本轮
            </button>
            <button
              type="button"
              disabled={submitting}
              onClick={() => void completeAwaitingNode()}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[var(--accent-primary)] px-3.5 text-ui font-medium text-[var(--accent-primary-fg)] transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5 fill-current" />
              <span>{isPlanner ? '确认计划并开始执行' : '完成当前阶段'}</span>
            </button>
          </div>
        </div>
      </div>
    )
  }

  // 4. Crashed / suspended / failed / aborted resumable checkpoint
  return (
    <div className="my-4 rounded-2xl border border-[#eed9b7] bg-[#fffaf0] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--status-warning)]" />
          <div>
            <div className="text-ui font-semibold text-[var(--text-primary)]">
              任务在节点「{primary.node_id}」暂停（{primary.kind}）
            </div>
            <div className="mt-0.5 text-caption text-[var(--text-secondary)]">
              {primary.failure?.message || '已保留断点与会话现场，可一键恢复继续执行或放弃本轮。'}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={submitting}
            onClick={() => void discardActiveThread()}
            className="inline-flex h-8 items-center rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-3 text-ui font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]"
          >
            放弃并收口
          </button>
          <button
            type="button"
            disabled={submitting}
            onClick={() => void resumeWithPrompt('')}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[var(--accent-primary)] px-3.5 text-ui font-medium text-[var(--accent-primary-fg)] hover:opacity-90"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            <span>恢复继续</span>
          </button>
        </div>
      </div>
    </div>
  )
}

function InteractionRequestCard({
  request,
  nodeId,
  answerText,
  setAnswerText,
  feedback,
  setFeedback,
  submitting,
  onRespond
}: {
  request: InteractionRequestEvent
  nodeId: string | null
  answerText: string
  setAnswerText: (v: string) => void
  feedback: string
  setFeedback: (v: string) => void
  submitting: boolean
  onRespond: (decision: 'approve' | 'deny' | 'cancel', value?: string, feedback?: string) => void
}): React.JSX.Element {
  const isUserInput = request.kind === 'user_input'

  return (
    <div className="my-4 rounded-2xl border border-[#e8d5b5] bg-[#fffcf5] p-4 shadow-xs">
      <div className="flex items-start gap-2.5">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-[var(--status-warning)]" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-ui font-semibold text-[var(--text-primary)]">
              {request.title || (isUserInput ? '需要你的输入' : '操作审批请求')}
            </span>
            {nodeId && (
              <span className="rounded-md bg-[var(--bg-subtle)] px-1.5 py-0.5 font-mono text-caption text-[var(--text-muted)]">
                {nodeId}
              </span>
            )}
          </div>
          {request.message && (
            <p className="mt-1 text-ui text-[var(--text-secondary)]">{request.message}</p>
          )}
          {request.tool_name && (
            <div className="mt-2 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-2.5">
              <div className="font-mono text-code font-medium text-[var(--text-primary)]">
                {request.tool_name}
              </div>
              {request.arguments && (
                <pre className="mt-1.5 overflow-x-auto font-mono text-code text-[var(--text-secondary)]">
                  {JSON.stringify(request.arguments, null, 2)}
                </pre>
              )}
            </div>
          )}

          {isUserInput ? (
            <div className="mt-3 space-y-2">
              {request.options && request.options.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {request.options.map((opt) => (
                    <button
                      key={opt}
                      type="button"
                      disabled={submitting}
                      onClick={() => onRespond('approve', opt)}
                      className="rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-3 py-1.5 text-ui font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-hover)]"
                    >
                      {opt}
                    </button>
                  ))}
                </div>
              ) : (
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={answerText}
                    onChange={(e) => setAnswerText(e.target.value)}
                    placeholder="输入你的回复..."
                    className="flex-1 rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-3 py-1.5 text-ui text-[var(--text-primary)] focus:outline-none"
                  />
                  <button
                    type="button"
                    disabled={submitting || !answerText.trim()}
                    onClick={() => onRespond('approve', answerText.trim())}
                    className="rounded-lg bg-[var(--accent-primary)] px-3.5 py-1.5 text-ui font-medium text-[var(--accent-primary-fg)] disabled:opacity-50"
                  >
                    提交
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="mt-3 space-y-2">
              <input
                type="text"
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder="可选：填写审批备注或拒绝原因..."
                className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-3 py-1.5 text-ui text-[var(--text-primary)] focus:outline-none"
              />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={submitting}
                  onClick={() => onRespond('approve', undefined, feedback.trim() || undefined)}
                  className="inline-flex h-8 items-center gap-1 rounded-lg bg-[var(--accent-primary)] px-3.5 text-ui font-medium text-[var(--accent-primary-fg)] hover:opacity-90"
                >
                  <Check className="h-3.5 w-3.5" />
                  <span>批准执行</span>
                </button>
                <button
                  type="button"
                  disabled={submitting}
                  onClick={() => onRespond('deny', undefined, feedback.trim() || undefined)}
                  className="inline-flex h-8 items-center gap-1 rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-3 text-ui font-medium text-[var(--status-danger)] hover:bg-[#fff5f5]"
                >
                  <X className="h-3.5 w-3.5" />
                  <span>拒绝</span>
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
