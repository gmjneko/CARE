import { SseFrameParser } from './sse-parser'
import type {
  AdapterInfo,
  HealthResponse,
  InteractionResponse,
  LocalSettingsResponse,
  LocalSettingsUpdate,
  ResumeThreadRequest,
  RunSnapshot,
  RuntimeEvent,
  StartRunRequest,
  ThreadState,
  ThreadSummary,
  WorkflowInfo
} from './runtime-types'

export class RuntimeApiError extends Error {
  public readonly status: number
  public readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'RuntimeApiError'
    this.status = status
    this.code = code
  }
}

export interface RuntimeClientConfig {
  baseUrl: string
  authToken: string | null
}

export interface StreamRunOptions {
  afterSeq?: number
  signal?: AbortSignal
  onEvent: (event: RuntimeEvent) => void
  onError?: (error: Error) => void
  onDone?: () => void
}

const TERMINAL_EVENT_TYPES = new Set([
  'workflow.completed',
  'workflow.interrupted',
  'runtime.error'
])

export class RuntimeClient {
  private baseUrl: string
  private authToken: string | null

  constructor(config: RuntimeClientConfig) {
    this.baseUrl = config.baseUrl.replace(/\/+$/, '')
    this.authToken = config.authToken
  }

  public configure(config: RuntimeClientConfig): void {
    this.baseUrl = config.baseUrl.replace(/\/+$/, '')
    this.authToken = config.authToken
  }

  public healthz(): Promise<HealthResponse> {
    return this.request<HealthResponse>('GET', '/healthz', { auth: false })
  }

  public listWorkflows(): Promise<WorkflowInfo[]> {
    return this.request<WorkflowInfo[]>('GET', '/workflows')
  }

  public listAdapters(): Promise<AdapterInfo[]> {
    return this.request<AdapterInfo[]>('GET', '/adapters')
  }

  public listThreads(params?: { workspaceDir?: string; limit?: number }): Promise<ThreadSummary[]> {
    const query = new URLSearchParams()
    if (params?.workspaceDir) query.set('workspace_dir', params.workspaceDir)
    if (params?.limit !== undefined) query.set('limit', String(params.limit))
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return this.request<ThreadSummary[]>('GET', `/threads${suffix}`)
  }

  public getThreadEvents(threadId: string): Promise<RuntimeEvent[]> {
    return this.request<RuntimeEvent[]>('GET', `/threads/${encodeURIComponent(threadId)}/events`)
  }

  public getThreadState(workflowId: string, threadId: string): Promise<ThreadState> {
    return this.request<ThreadState>(
      'GET',
      `/workflows/${encodeURIComponent(workflowId)}/threads/${encodeURIComponent(threadId)}`
    )
  }

  public startRun(body: StartRunRequest): Promise<RunSnapshot> {
    return this.request<RunSnapshot>('POST', '/runs', { body })
  }

  public resumeThread(
    workflowId: string,
    threadId: string,
    body: ResumeThreadRequest
  ): Promise<RunSnapshot> {
    return this.request<RunSnapshot>(
      'POST',
      `/workflows/${encodeURIComponent(workflowId)}/threads/${encodeURIComponent(threadId)}/resume`,
      { body }
    )
  }

  public discardThread(workflowId: string, threadId: string): Promise<ThreadState> {
    return this.request<ThreadState>(
      'POST',
      `/workflows/${encodeURIComponent(workflowId)}/threads/${encodeURIComponent(threadId)}/discard`
    )
  }

  public steerRun(runId: string, message: string, nodeId?: string): Promise<void> {
    return this.request<void>('POST', `/runs/${encodeURIComponent(runId)}/steer`, {
      body: { message, node_id: nodeId ?? null }
    })
  }

  public followUpRun(runId: string, message: string, nodeId?: string): Promise<void> {
    return this.request<void>('POST', `/runs/${encodeURIComponent(runId)}/follow-up`, {
      body: { message, node_id: nodeId ?? null }
    })
  }

  public abortRun(runId: string): Promise<void> {
    return this.request<void>('POST', `/runs/${encodeURIComponent(runId)}/abort`)
  }

  public suspendRun(runId: string): Promise<void> {
    return this.request<void>('POST', `/runs/${encodeURIComponent(runId)}/suspend`)
  }

  public respondInteraction(
    runId: string,
    response: InteractionResponse,
    nodeId?: string
  ): Promise<void> {
    return this.request<void>('POST', `/runs/${encodeURIComponent(runId)}/interactions`, {
      body: { response, node_id: nodeId ?? null }
    })
  }

  public getSettings(): Promise<LocalSettingsResponse> {
    return this.request<LocalSettingsResponse>('GET', '/settings')
  }

  public updateSettings(body: LocalSettingsUpdate): Promise<LocalSettingsResponse> {
    return this.request<LocalSettingsResponse>('PUT', '/settings', { body })
  }

  /**
   * Subscribe to a run's SSE event stream using fetch + ReadableStream so Authorization headers
   * and Last-Event-ID replay work reliably in Electron.
   */
  public async streamRunEvents(runId: string, options: StreamRunOptions): Promise<void> {
    let lastSeq = options.afterSeq ?? 0
    let attempts = 0
    const maxRetries = 3

    while (!options.signal?.aborted) {
      const headers: Record<string, string> = {
        Accept: 'text/event-stream'
      }
      if (this.authToken) {
        headers.Authorization = `Bearer ${this.authToken}`
      }
      if (lastSeq > 0) {
        headers['Last-Event-ID'] = String(lastSeq)
      }

      const url = `${this.baseUrl}/runs/${encodeURIComponent(runId)}/events`
      let sawTerminal = false

      try {
        const response = await fetch(url, {
          method: 'GET',
          headers,
          signal: options.signal
        })

        if (!response.ok) {
          throw await this.parseHttpError(response)
        }
        if (!response.body) {
          throw new Error('SSE response body is empty')
        }

        attempts = 0
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        const parser = new SseFrameParser()

        try {
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            const text = decoder.decode(value, { stream: true })
            for (const frame of parser.feed(text)) {
              const ev = JSON.parse(frame.data) as RuntimeEvent
              if (ev.seq > lastSeq) lastSeq = ev.seq
              if (ev.payload.type && TERMINAL_EVENT_TYPES.has(ev.payload.type)) sawTerminal = true
              options.onEvent(ev)
            }
          }
          const remaining = decoder.decode()
          for (const frame of [...parser.feed(remaining), ...parser.flush()]) {
            const ev = JSON.parse(frame.data) as RuntimeEvent
            if (ev.seq > lastSeq) lastSeq = ev.seq
            if (ev.payload.type && TERMINAL_EVENT_TYPES.has(ev.payload.type)) sawTerminal = true
            options.onEvent(ev)
          }
        } finally {
          reader.releaseLock()
        }

        options.onDone?.()
        return
      } catch (err) {
        if (options.signal?.aborted) {
          return
        }
        if (err instanceof RuntimeApiError && err.status >= 400 && err.status < 500) {
          options.onError?.(err)
          return
        }
        if (sawTerminal) {
          options.onDone?.()
          return
        }
        attempts += 1
        if (attempts > maxRetries) {
          options.onError?.(err instanceof Error ? err : new Error(String(err)))
          return
        }
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 400 * attempts))
      }
    }
  }

  private async request<T>(
    method: string,
    path: string,
    options?: { body?: unknown; auth?: boolean }
  ): Promise<T> {
    const headers: Record<string, string> = {}
    if (options?.auth !== false && this.authToken) {
      headers.Authorization = `Bearer ${this.authToken}`
    }
    if (options?.body !== undefined) {
      headers['Content-Type'] = 'application/json'
    }

    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: options?.body !== undefined ? JSON.stringify(options.body) : undefined
    })

    if (!response.ok) {
      throw await this.parseHttpError(response)
    }
    if (response.status === 204) {
      return undefined as T
    }
    return (await response.json()) as T
  }

  private async parseHttpError(response: Response): Promise<RuntimeApiError> {
    try {
      const data = (await response.json()) as {
        error?: { code?: string; message?: string }
      }
      if (data?.error?.message) {
        return new RuntimeApiError(
          response.status,
          data.error.code ?? `http_${response.status}`,
          data.error.message
        )
      }
    } catch {
      // Fall through to status text
    }
    return new RuntimeApiError(
      response.status,
      `http_${response.status}`,
      response.statusText || `HTTP ${response.status}`
    )
  }
}
