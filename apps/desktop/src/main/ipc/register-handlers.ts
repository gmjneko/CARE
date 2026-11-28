import { ipcMain } from 'electron'
import { IPC_CHANNELS, type ExternalAppKind } from '@shared/ipc-contracts'
import type { SidecarManager } from '../sidecar/sidecar-manager'
import {
  getGitFileDiff,
  getGitSummary,
  listWorkspaceFiles,
  openInExternalApp,
  pickWorkspaceDirectory,
  readWorkspaceFile
} from '../workspace/local-tools'

export function registerIpcHandlers(sidecar: SidecarManager): void {
  ipcMain.handle(IPC_CHANNELS.RUNTIME_GET_CONNECTION, () => sidecar.ensureStarted())
  ipcMain.handle(IPC_CHANNELS.RUNTIME_RESTART, () => sidecar.restart())
  ipcMain.handle(IPC_CHANNELS.RUNTIME_GET_LOGS, () => sidecar.getLogs())

  ipcMain.handle(IPC_CHANNELS.WORKSPACE_PICK_DIR, () => pickWorkspaceDirectory())
  ipcMain.handle(IPC_CHANNELS.WORKSPACE_GIT_SUMMARY, (_event, workspaceDir: string) =>
    getGitSummary(workspaceDir)
  )
  ipcMain.handle(
    IPC_CHANNELS.WORKSPACE_GIT_DIFF,
    (_event, workspaceDir: string, filePath: string) => getGitFileDiff(workspaceDir, filePath)
  )
  ipcMain.handle(
    IPC_CHANNELS.WORKSPACE_LIST_FILES,
    (_event, workspaceDir: string, relativePath?: string) =>
      listWorkspaceFiles(workspaceDir, relativePath)
  )
  ipcMain.handle(
    IPC_CHANNELS.WORKSPACE_READ_FILE,
    (_event, workspaceDir: string, relativePath: string) =>
      readWorkspaceFile(workspaceDir, relativePath)
  )
  ipcMain.handle(
    IPC_CHANNELS.WORKSPACE_OPEN_EXTERNAL,
    (_event, workspaceDir: string, app: ExternalAppKind) => openInExternalApp(workspaceDir, app)
  )
}
