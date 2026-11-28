import { contextBridge, ipcRenderer } from 'electron'
import { IPC_CHANNELS, type CareDesktopBridge, type ExternalAppKind } from '@shared/ipc-contracts'

const bridge: CareDesktopBridge = {
  platform: process.platform,
  getRuntimeConnection: () => ipcRenderer.invoke(IPC_CHANNELS.RUNTIME_GET_CONNECTION),
  restartSidecar: () => ipcRenderer.invoke(IPC_CHANNELS.RUNTIME_RESTART),
  getSidecarLogs: () => ipcRenderer.invoke(IPC_CHANNELS.RUNTIME_GET_LOGS),
  pickWorkspaceDirectory: () => ipcRenderer.invoke(IPC_CHANNELS.WORKSPACE_PICK_DIR),
  getGitSummary: (workspaceDir: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.WORKSPACE_GIT_SUMMARY, workspaceDir),
  getGitFileDiff: (workspaceDir: string, filePath: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.WORKSPACE_GIT_DIFF, workspaceDir, filePath),
  listWorkspaceFiles: (workspaceDir: string, relativePath?: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.WORKSPACE_LIST_FILES, workspaceDir, relativePath),
  readWorkspaceFile: (workspaceDir: string, relativePath: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.WORKSPACE_READ_FILE, workspaceDir, relativePath),
  openInExternalApp: (workspaceDir: string, app: ExternalAppKind) =>
    ipcRenderer.invoke(IPC_CHANNELS.WORKSPACE_OPEN_EXTERNAL, workspaceDir, app)
}

contextBridge.exposeInMainWorld('careDesktop', bridge)
