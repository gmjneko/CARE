import { create } from 'zustand'
import type { ThinkingLevel } from '@renderer/sdk/runtime-types'
import type { InspectorDetailDescriptor } from '@renderer/shell/contracts'

const WORKSPACES_STORAGE_KEY = 'care.desktop.saved_workspaces'
const LAST_WORKSPACE_KEY = 'care.desktop.selected_workspace'

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

interface WorkbenchStoreState {
  activePageId: string
  sidebarOpen: boolean
  inspectorOpen: boolean
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
  toggleInspector: () => void
  setInspectorOpen: (open: boolean) => void
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
  inspectorOpen: true,
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
  toggleInspector: () =>
    set((s) => ({
      inspectorOpen: !s.inspectorOpen,
      inspectorDetail: s.inspectorOpen ? null : s.inspectorDetail
    })),
  setInspectorOpen: (open) =>
    set((s) => ({
      inspectorOpen: open,
      inspectorDetail: open ? s.inspectorDetail : null
    })),
  openInspectorDetail: (detail) => set({ inspectorOpen: true, inspectorDetail: detail }),
  closeInspectorDetail: () => set({ inspectorDetail: null }),
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
