import { create } from 'zustand'
import type { ThinkingLevel } from '@renderer/sdk/runtime-types'
import type { InspectorDetailDescriptor } from '@renderer/shell/contracts'

const WORKSPACES_STORAGE_KEY = 'care.desktop.saved_workspaces'
const LAST_WORKSPACE_KEY = 'care.desktop.selected_workspace'
const SIDEBAR_WIDTH_KEY = 'care.desktop.sidebar_width'
const RIGHT_PANEL_WIDTH_KEY = 'care.desktop.right_panel_width'

export const DEFAULT_SIDEBAR_WIDTH = 248
export const MIN_SIDEBAR_WIDTH = 200
export const MAX_SIDEBAR_WIDTH = 380

export const DEFAULT_RIGHT_PANEL_WIDTH = 480
export const MIN_RIGHT_PANEL_WIDTH = 300
export const MAX_RIGHT_PANEL_WIDTH = 760

function loadNumberSetting(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return fallback
    const parsed = Number.parseInt(raw, 10)
    if (Number.isNaN(parsed)) return fallback
    return Math.max(min, Math.min(max, parsed))
  } catch {
    return fallback
  }
}

function persistNumberSetting(key: string, value: number): void {
  try {
    window.localStorage.setItem(key, String(Math.round(value)))
  } catch {
    // Ignore storage errors
  }
}

function loadSavedWorkspaces(): string[] {
  try {
    const raw = window.localStorage.getItem(WORKSPACES_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

function loadLastWorkspace(): string | null {
  try {
    return window.localStorage.getItem(LAST_WORKSPACE_KEY) || null
  } catch {
    return null
  }
}

function persistWorkspaces(dirs: string[]): void {
  try {
    window.localStorage.setItem(WORKSPACES_STORAGE_KEY, JSON.stringify(dirs))
  } catch {
    // Ignore storage errors
  }
}

export type InspectorMode = 'closed' | 'floating' | 'panel'

interface WorkbenchStoreState {
  activePageId: string
  sidebarOpen: boolean
  sidebarWidth: number
  inspectorMode: InspectorMode
  rightPanelWidth: number
  inspectorDetail: InspectorDetailDescriptor | null
  settingsOpen: boolean
  notificationsOpen: boolean
  searchOpen: boolean
  searchQuery: string

  savedWorkspaces: string[]
  selectedWorkspaceDir: string | null
  selectedWorkflowId: string
  selectedProfile: string | null
  selectedThinkingLevel: ThinkingLevel | null

  setActivePage: (pageId: string) => void
  toggleSidebar: () => void
  setSidebarWidth: (width: number) => void
  toggleFloatingInspector: () => void
  toggleRightPanel: () => void
  setRightPanelWidth: (width: number) => void
  setInspectorMode: (mode: InspectorMode) => void
  openInspectorDetail: (detail: InspectorDetailDescriptor) => void
  closeInspectorDetail: () => void
  setSettingsOpen: (open: boolean) => void
  setNotificationsOpen: (open: boolean) => void
  setSearchOpen: (open: boolean) => void
  setSearchQuery: (q: string) => void

  addWorkspace: (dir: string) => void
  setSelectedWorkspace: (dir: string | null) => void
  setSelectedWorkflow: (workflowId: string) => void
  setSelectedProfile: (profile: string | null) => void
  setSelectedThinkingLevel: (level: ThinkingLevel | null) => void
}

export const useWorkbenchStore = create<WorkbenchStoreState>((set, get) => ({
  activePageId: 'chat',
  sidebarOpen: true,
  sidebarWidth: loadNumberSetting(
    SIDEBAR_WIDTH_KEY,
    DEFAULT_SIDEBAR_WIDTH,
    MIN_SIDEBAR_WIDTH,
    MAX_SIDEBAR_WIDTH
  ),
  inspectorMode: 'floating',
  rightPanelWidth: loadNumberSetting(
    RIGHT_PANEL_WIDTH_KEY,
    DEFAULT_RIGHT_PANEL_WIDTH,
    MIN_RIGHT_PANEL_WIDTH,
    MAX_RIGHT_PANEL_WIDTH
  ),
  inspectorDetail: null,
  settingsOpen: false,
  notificationsOpen: false,
  searchOpen: false,
  searchQuery: '',

  savedWorkspaces: loadSavedWorkspaces(),
  selectedWorkspaceDir: loadLastWorkspace(),
  selectedWorkflowId: 'single_agent',
  selectedProfile: null,
  selectedThinkingLevel: null,

  setActivePage: (pageId) => set({ activePageId: pageId, inspectorDetail: null }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarWidth: (width) => {
    const clamped = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, Math.round(width)))
    persistNumberSetting(SIDEBAR_WIDTH_KEY, clamped)
    set({ sidebarWidth: clamped })
  },
  toggleFloatingInspector: () =>
    set((s) => ({
      inspectorMode: s.inspectorMode === 'floating' ? 'closed' : 'floating',
      inspectorDetail: null
    })),
  toggleRightPanel: () =>
    set((s) => ({
      inspectorMode: s.inspectorMode === 'panel' ? 'closed' : 'panel'
    })),
  setRightPanelWidth: (width) => {
    const clamped = Math.max(
      MIN_RIGHT_PANEL_WIDTH,
      Math.min(MAX_RIGHT_PANEL_WIDTH, Math.round(width))
    )
    persistNumberSetting(RIGHT_PANEL_WIDTH_KEY, clamped)
    set({ rightPanelWidth: clamped })
  },
  setInspectorMode: (mode) =>
    set((s) => ({
      inspectorMode: mode,
      inspectorDetail: mode === 'panel' ? s.inspectorDetail : null
    })),
  openInspectorDetail: (detail) => set({ inspectorMode: 'panel', inspectorDetail: detail }),
  closeInspectorDetail: () => set({ inspectorMode: 'floating', inspectorDetail: null }),
  setSettingsOpen: (open) => set({ settingsOpen: open }),
  setNotificationsOpen: (open) => set({ notificationsOpen: open }),
  setSearchOpen: (open) =>
    set((s) => ({ searchOpen: open, searchQuery: open ? s.searchQuery : '' })),
  setSearchQuery: (searchQuery) => set({ searchQuery }),

  addWorkspace: (dir) => {
    const clean = dir.trim()
    if (!clean) return
    const current = get().savedWorkspaces
    const next = current.includes(clean) ? current : [clean, ...current]
    persistWorkspaces(next)
    try {
      window.localStorage.setItem(LAST_WORKSPACE_KEY, clean)
    } catch {
      // Ignore
    }
    set({ savedWorkspaces: next, selectedWorkspaceDir: clean })
  },

  setSelectedWorkspace: (dir) => {
    try {
      if (dir) window.localStorage.setItem(LAST_WORKSPACE_KEY, dir)
      else window.localStorage.removeItem(LAST_WORKSPACE_KEY)
    } catch {
      // Ignore
    }
    set({ selectedWorkspaceDir: dir })
  },

  setSelectedWorkflow: (workflowId) => set({ selectedWorkflowId: workflowId }),
  setSelectedProfile: (profile) => set({ selectedProfile: profile }),
  setSelectedThinkingLevel: (level) => set({ selectedThinkingLevel: level })
}))
