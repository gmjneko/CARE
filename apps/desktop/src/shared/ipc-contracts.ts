/**
 * Shared IPC contracts between Electron Main, Preload, and Renderer processes.
 * Zero runtime dependencies — types and channel constants only.
 */

export const IPC_CHANNELS = {
  RUNTIME_GET_CONNECTION: 'care:runtime:get-connection',
  RUNTIME_RESTART: 'care:runtime:restart',
  RUNTIME_GET_LOGS: 'care:runtime:get-logs',
  WORKSPACE_PICK_DIR: 'care:workspace:pick-dir',
  WORKSPACE_GIT_SUMMARY: 'care:workspace:git-summary',
  WORKSPACE_GIT_DIFF: 'care:workspace:git-diff',
  WORKSPACE_LIST_FILES: 'care:workspace:list-files',
  WORKSPACE_READ_FILE: 'care:workspace:read-file',
  WORKSPACE_OPEN_EXTERNAL: 'care:workspace:open-external'
} as const

export type SidecarStatus = 'starting' | 'ready' | 'error' | 'stopped' | 'attached'

export interface RuntimeConnectionInfo {
  baseUrl: string
  authToken: string | null
  status: SidecarStatus
  mode: 'managed' | 'attached'
  error: string | null
}

export type ExternalAppKind = 'finder' | 'terminal' | 'vscode' | 'cursor' | 'idea'

export interface GitFileChange {
  path: string
  status: string
  additions: number
  deletions: number
}

export interface GitWorkspaceSummary {
  isGitRepo: boolean
  branch: string | null
  files: GitFileChange[]
  totalAdditions: number
  totalDeletions: number
}

export interface WorkspaceFileEntry {
  name: string
  relativePath: string
  isDirectory: boolean
}

export interface WorkspaceFileContent {
  content: string
  truncated: boolean
}

export interface CareDesktopBridge {
  platform: NodeJS.Platform
  getRuntimeConnection: () => Promise<RuntimeConnectionInfo>
  restartSidecar: () => Promise<RuntimeConnectionInfo>
  getSidecarLogs: () => Promise<string[]>
  pickWorkspaceDirectory: () => Promise<string | null>
  getGitSummary: (workspaceDir: string) => Promise<GitWorkspaceSummary>
  getGitFileDiff: (workspaceDir: string, filePath: string) => Promise<string>
  listWorkspaceFiles: (workspaceDir: string, relativePath?: string) => Promise<WorkspaceFileEntry[]>
  readWorkspaceFile: (workspaceDir: string, relativePath: string) => Promise<WorkspaceFileContent>
  openInExternalApp: (workspaceDir: string, app: ExternalAppKind) => Promise<void>
}
