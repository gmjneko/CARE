import * as Dialog from '@radix-ui/react-dialog'
import { AlertCircle, Bell, PauseCircle, X } from 'lucide-react'
import { useSessionStore } from '@renderer/stores/session-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'

export function NotificationsDialog(): React.JSX.Element {
  const open = useWorkbenchStore((s) => s.notificationsOpen)
  const setOpen = useWorkbenchStore((s) => s.setNotificationsOpen)
  const threads = useSessionStore((s) => s.threads)
  const openThread = useSessionStore((s) => s.openThread)

  const pending = threads.filter((t) => t.status === 'interrupted' || t.status === 'crashed')

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="care-overlay fixed inset-0 z-50 bg-black/15" />
        <Dialog.Content className="care-dialog fixed left-1/2 top-1/2 z-50 w-[460px] max-w-[92vw] -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] shadow-[var(--shadow-floating)]">
          <div className="flex items-center justify-between border-b border-[var(--border-hairline)] px-4 py-3">
            <Dialog.Title className="inline-flex items-center gap-2 text-heading font-semibold text-[var(--text-primary)]">
              <Bell className="h-4 w-4 text-[var(--text-secondary)]" />
              <span>待处理提醒</span>
            </Dialog.Title>
            <Dialog.Close className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-[var(--text-muted)] hover:bg-[var(--bg-hover)]">
              <X className="h-4 w-4" />
            </Dialog.Close>
          </div>
          <div className="max-h-[60vh] overflow-y-auto p-3">
            {pending.length === 0 ? (
              <div className="py-10 text-center text-ui text-[var(--text-muted)]">
                暂无等待审批、确认或恢复的任务
              </div>
            ) : (
              <div className="space-y-1.5">
                {pending.map((thread) => (
                  <button
                    key={thread.thread_id}
                    type="button"
                    onClick={() => {
                      setOpen(false)
                      void openThread(thread)
                    }}
                    className="flex w-full items-start gap-2.5 rounded-xl border border-[var(--border-hairline)] p-3 text-left transition-colors hover:bg-[var(--bg-hover)]"
                  >
                    {thread.status === 'crashed' ? (
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--status-danger)]" />
                    ) : (
                      <PauseCircle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--status-warning)]" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-ui font-medium text-[var(--text-primary)]">
                        {thread.title}
                      </div>
                      <div className="mt-0.5 text-caption text-[var(--text-muted)]">
                        {thread.workflow_id} ·{' '}
                        {thread.status === 'crashed'
                          ? '异常中断，可恢复或放弃'
                          : '等待你的确认或输入'}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
