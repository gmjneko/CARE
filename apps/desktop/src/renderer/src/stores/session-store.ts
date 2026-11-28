import { create } from 'zustand'
import { mergeCanonicalEvents } from '@renderer/sdk/event-projection'
import type {
  InteractionResponse,
  RuntimeEvent,
  ThreadState,
  ThreadSummary
} from '@renderer/sdk/runtime-types'
import { useRuntimeStore } from './runtime-store'
import { useWorkbenchStore } from './workbench-store'

interface SessionStoreState {
  threads: ThreadSummary[]
  loadingThreads: boolean

  activeThreadId: string | null
  activeWorkflowId: string | null
  activeWorkspaceDir: string | null
  activeRunId: string | null
  threadState: ThreadState | null
  events: RuntimeEvent[]
  loadingSession: boolean
  submitting: boolean
  error: string | null

  refreshThreads: () => Promise<void>
  startNewTask: () => void
  openThread: (thread: ThreadSummary) => Promise<void>
  submitPrompt: (prompt: string) => Promise<void>
  sendFollowUp: (message: string) => Promise<void>
  sendSteer: (message: string) => Promise<void>
  abortActiveRun: () => Promise<void>
  suspendActiveRun: () => Promise<void>
  resumeWithPrompt: (prompt: string) => Promise<void>
  completeAwaitingNode: () => Promise<void>
  respondInteraction: (response: InteractionResponse, nodeId?: string) => Promise<void>
  discardActiveThread: () => Promise<void>
  clearError: () => void
}

let activeAbortController: AbortController | null = null
let pendingEventsBuffer: RuntimeEvent[] = []
let rafScheduled = false

function cancelActiveStream(): void {
  if (activeAbortController) {
    activeAbortController.abort()
    activeAbortController = null
  }
  pendingEventsBuffer = []
  rafScheduled = false
}

function createThreadId(): string {
  return `thread-${crypto.randomUUID().slice(0, 12)}`
}

export const useSessionStore = create<SessionStoreState>((set, get) => {
  const flushBufferedEvents = (): void => {
    rafScheduled = false
    if (pendingEventsBuffer.length === 0) return
    const batch = pendingEventsBuffer
    pendingEventsBuffer = []
    set((s) => ({ events: mergeCanonicalEvents(s.events, batch) }))
  }

  const enqueueEvent = (ev: RuntimeEvent): void => {
    pendingEventsBuffer.push(ev)
    if (!ev.is_ephemeral) {
      flushBufferedEvents()
      return
    }
    if (!rafScheduled) {
      rafScheduled = true
      requestAnimationFrame(flushBufferedEvents)
    }
  }

  const attachRunStream = (
    runId: string,
    threadId: string,
    workflowId: string,
    afterSeq = 0
  ): void => {
    cancelActiveStream()
    const client = useRuntimeStore.getState().client
    if (!client) return

    const controller = new AbortController()
    activeAbortController = controller
    set({ activeRunId: runId, error: null })

    void client.streamRunEvents(runId, {
      afterSeq,
      signal: controller.signal,
      onEvent: (ev) => {
        if (get().activeThreadId !== threadId) return
        enqueueEvent(ev)
      },
      onError: (err) => {
        if (get().activeThreadId !== threadId) return
        flushBufferedEvents()
        set({ error: err.message, activeRunId: null })
        void get().refreshThreads()
      },
      onDone: async () => {
        if (get().activeThreadId !== threadId) return
        flushBufferedEvents()
        try {
          const state = await client.getThreadState(workflowId, threadId)
          if (get().activeThreadId === threadId) {
            set({
              threadState: state,
              activeRunId: state.active_run_id
            })
          }
        } catch {
          if (get().activeThreadId === threadId) {
            set({ activeRunId: null })
          }
        }
        await get().refreshThreads()
      }
    })
  }

  return {
    threads: [],
    loadingThreads: false,

    activeThreadId: null,
    activeWorkflowId: null,
    activeWorkspaceDir: null,
    activeRunId: null,
    threadState: null,
    events: [],
    loadingSession: false,
    submitting: false,
    error: null,

    clearError: () => set({ error: null }),

    refreshThreads: async () => {
      const client = useRuntimeStore.getState().client
      if (!client) return
      set({ loadingThreads: true })
      try {
        const threads = await client.listThreads({ limit: 100 })
        set({ threads, loadingThreads: false })
        const wb = useWorkbenchStore.getState()
        for (const t of threads) {
          if (t.workspace_dir && !wb.savedWorkspaces.includes(t.workspace_dir)) {
            wb.addWorkspace(t.workspace_dir)
          }
        }
      } catch (err) {
        set({
          loadingThreads: false,
          error: err instanceof Error ? err.message : String(err)
        })
      }
    },

    startNewTask: () => {
      cancelActiveStream()
      useWorkbenchStore.getState().setActivePage('chat')
      set({
        activeThreadId: null,
        activeWorkflowId: null,
        activeWorkspaceDir: null,
        activeRunId: null,
        threadState: null,
        events: [],
        error: null
      })
    },

    openThread: async (thread) => {
      const client = useRuntimeStore.getState().client
      if (!client) return
      cancelActiveStream()
      const wb = useWorkbenchStore.getState()
      wb.setActivePage('chat')
      wb.setSelectedWorkspace(thread.workspace_dir)
      wb.setSelectedWorkflow(thread.workflow_id)

      set({
        activeThreadId: thread.thread_id,
        activeWorkflowId: thread.workflow_id,
        activeWorkspaceDir: thread.workspace_dir,
        activeRunId: thread.active_run_id,
        loadingSession: true,
        events: [],
        threadState: null,
        error: null
      })

      try {
        const [events, state] = await Promise.all([
          client.getThreadEvents(thread.thread_id),
          client.getThreadState(thread.workflow_id, thread.thread_id)
        ])
        if (get().activeThreadId !== thread.thread_id) return
        set({
          events,
          threadState: state,
          activeRunId: state.active_run_id,
          loadingSession: false
        })

        if (state.active_run_id) {
          const runEvents = events.filter((e) => e.run_id === state.active_run_id)
          const lastSeq = runEvents.length > 0 ? runEvents[runEvents.length - 1].seq : 0
          attachRunStream(state.active_run_id, thread.thread_id, thread.workflow_id, lastSeq)
        }
      } catch (err) {
        if (get().activeThreadId !== thread.thread_id) return
        set({
          loadingSession: false,
          error: err instanceof Error ? err.message : String(err)
        })
      }
    },

    submitPrompt: async (prompt) => {
      const trimmed = prompt.trim()
      if (!trimmed) return
      const client = useRuntimeStore.getState().client
      if (!client) {
        set({ error: '本地 Runtime 尚未就绪' })
        return
      }

      const wb = useWorkbenchStore.getState()
      const state = get()

      // If current thread is paused in awaiting_input or resumable interrupt, route to resume
      if (state.activeThreadId && state.threadState?.resumable) {
        await state.resumeWithPrompt(trimmed)
        return
      }

      const workspaceDir = state.activeWorkspaceDir || wb.selectedWorkspaceDir
      if (!workspaceDir) {
        set({ error: '请先在下方选择一个本地项目目录' })
        return
      }

      const workflowId = state.activeWorkflowId || wb.selectedWorkflowId
      const threadId = state.activeThreadId || createThreadId()

      set({
        submitting: true,
        error: null,
        activeThreadId: threadId,
        activeWorkflowId: workflowId,
        activeWorkspaceDir: workspaceDir
      })

      try {
        const snap = await client.startRun({
          workflow_id: workflowId,
          prompt: trimmed,
          workspace_dir: workspaceDir,
          thread_id: threadId,
          profile: wb.selectedProfile,
          thinking_level: wb.selectedThinkingLevel,
          persist_session: true,
          interaction_mode: 'wait'
        })
        wb.addWorkspace(workspaceDir)
        set({ submitting: false })
        attachRunStream(snap.run_id, snap.thread_id, snap.workflow_id, 0)
        void get().refreshThreads()
      } catch (err) {
        set({
          submitting: false,
          error: err instanceof Error ? err.message : String(err)
        })
      }
    },

    sendFollowUp: async (message) => {
      const trimmed = message.trim()
      const { activeRunId } = get()
      const client = useRuntimeStore.getState().client
      if (!trimmed || !activeRunId || !client) return
      try {
        await client.followUpRun(activeRunId, trimmed)
      } catch (err) {
        set({ error: err instanceof Error ? err.message : String(err) })
      }
    },

    sendSteer: async (message) => {
      const trimmed = message.trim()
      const { activeRunId } = get()
      const client = useRuntimeStore.getState().client
      if (!trimmed || !activeRunId || !client) return
      try {
        await client.steerRun(activeRunId, trimmed)
      } catch (err) {
        set({ error: err instanceof Error ? err.message : String(err) })
      }
    },

    abortActiveRun: async () => {
      const { activeRunId } = get()
      const client = useRuntimeStore.getState().client
      if (!activeRunId || !client) return
      try {
        await client.abortRun(activeRunId)
      } catch (err) {
        set({ error: err instanceof Error ? err.message : String(err) })
      }
    },

    suspendActiveRun: async () => {
      const { activeRunId } = get()
      const client = useRuntimeStore.getState().client
      if (!activeRunId || !client) return
      try {
        await client.suspendRun(activeRunId)
      } catch (err) {
        set({ error: err instanceof Error ? err.message : String(err) })
      }
    },

    resumeWithPrompt: async (prompt) => {
      const { activeThreadId, activeWorkflowId } = get()
      const client = useRuntimeStore.getState().client
      if (!activeThreadId || !activeWorkflowId || !client) return
      const wb = useWorkbenchStore.getState()
      set({ submitting: true, error: null })
      try {
        const snap = await client.resumeThread(activeWorkflowId, activeThreadId, {
          prompt: prompt.trim() || null,
          profile: wb.selectedProfile,
          thinking_level: wb.selectedThinkingLevel,
          interaction_mode: 'wait'
        })
        set({ submitting: false })
        attachRunStream(snap.run_id, snap.thread_id, snap.workflow_id, 0)
        void get().refreshThreads()
      } catch (err) {
        set({ submitting: false, error: err instanceof Error ? err.message : String(err) })
      }
    },

    completeAwaitingNode: async () => {
      const { activeThreadId, activeWorkflowId } = get()
      const client = useRuntimeStore.getState().client
      if (!activeThreadId || !activeWorkflowId || !client) return
      set({ submitting: true, error: null })
      try {
        const snap = await client.resumeThread(activeWorkflowId, activeThreadId, {
          complete: true,
          interaction_mode: 'wait'
        })
        set({ submitting: false })
        attachRunStream(snap.run_id, snap.thread_id, snap.workflow_id, 0)
        void get().refreshThreads()
      } catch (err) {
        set({ submitting: false, error: err instanceof Error ? err.message : String(err) })
      }
    },

    respondInteraction: async (response, nodeId) => {
      const { activeRunId, activeThreadId, activeWorkflowId } = get()
      const client = useRuntimeStore.getState().client
      if (!client) return
      set({ error: null })
      try {
        if (activeRunId) {
          await client.respondInteraction(activeRunId, response, nodeId)
          return
        }
        if (activeThreadId && activeWorkflowId) {
          const snap = await client.resumeThread(activeWorkflowId, activeThreadId, {
            node_id: nodeId ?? null,
            interaction_response: response,
            interaction_mode: 'wait'
          })
          attachRunStream(snap.run_id, snap.thread_id, snap.workflow_id, 0)
          void get().refreshThreads()
        }
      } catch (err) {
        set({ error: err instanceof Error ? err.message : String(err) })
      }
    },

    discardActiveThread: async () => {
      const { activeThreadId, activeWorkflowId } = get()
      const client = useRuntimeStore.getState().client
      if (!activeThreadId || !activeWorkflowId || !client) return
      try {
        const state = await client.discardThread(activeWorkflowId, activeThreadId)
        set({ threadState: state, activeRunId: null, error: null })
        await get().refreshThreads()
      } catch (err) {
        set({ error: err instanceof Error ? err.message : String(err) })
      }
    }
  }
})
