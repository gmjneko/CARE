import { create } from 'zustand'
import type { RuntimeConnectionInfo } from '@shared/ipc-contracts'
import { RuntimeClient } from '@renderer/sdk/runtime-client'
import type {
  AdapterInfo,
  LocalSettingsResponse,
  LocalSettingsUpdate,
  WorkflowInfo
} from '@renderer/sdk/runtime-types'

interface RuntimeStoreState {
  connection: RuntimeConnectionInfo | null
  client: RuntimeClient | null
  workflows: WorkflowInfo[]
  adapters: AdapterInfo[]
  settings: LocalSettingsResponse | null
  initializing: boolean
  error: string | null

  initialize: () => Promise<void>
  restartSidecar: () => Promise<void>
  refreshMetadata: () => Promise<void>
  saveSettings: (update: LocalSettingsUpdate) => Promise<LocalSettingsResponse>
}

export const useRuntimeStore = create<RuntimeStoreState>((set, get) => ({
  connection: null,
  client: null,
  workflows: [],
  adapters: [],
  settings: null,
  initializing: false,
  error: null,

  initialize: async () => {
    if (get().initializing) return
    set({ initializing: true, error: null })
    try {
      const conn = await window.careDesktop.getRuntimeConnection()
      if (conn.status === 'error' || !conn.baseUrl) {
        set({
          connection: conn,
          client: null,
          initializing: false,
          error: conn.error ?? '无法连接本地 Agent Runtime'
        })
        return
      }
      const client =
        get().client ?? new RuntimeClient({ baseUrl: conn.baseUrl, authToken: conn.authToken })
      client.configure({ baseUrl: conn.baseUrl, authToken: conn.authToken })
      set({ connection: conn, client })
      await get().refreshMetadata()
      set({ initializing: false })
    } catch (err) {
      set({
        initializing: false,
        error: err instanceof Error ? err.message : String(err)
      })
    }
  },

  restartSidecar: async () => {
    set({ initializing: true, error: null })
    try {
      const conn = await window.careDesktop.restartSidecar()
      if (conn.status === 'error' || !conn.baseUrl) {
        set({
          connection: conn,
          initializing: false,
          error: conn.error ?? '重启本地 Agent Runtime 失败'
        })
        return
      }
      const client =
        get().client ?? new RuntimeClient({ baseUrl: conn.baseUrl, authToken: conn.authToken })
      client.configure({ baseUrl: conn.baseUrl, authToken: conn.authToken })
      set({ connection: conn, client })
      await get().refreshMetadata()
      set({ initializing: false })
    } catch (err) {
      set({
        initializing: false,
        error: err instanceof Error ? err.message : String(err)
      })
    }
  },

  refreshMetadata: async () => {
    const client = get().client
    if (!client) return
    const [workflows, adapters, settings] = await Promise.all([
      client.listWorkflows(),
      client.listAdapters(),
      client.getSettings().catch(() => null)
    ])
    set({ workflows, adapters, settings, error: null })
  },

  saveSettings: async (update: LocalSettingsUpdate) => {
    const client = get().client
    if (!client) {
      throw new Error('本地 Runtime 尚未就绪')
    }
    const saved = await client.updateSettings(update)
    set({ settings: saved })
    return saved
  }
}))
