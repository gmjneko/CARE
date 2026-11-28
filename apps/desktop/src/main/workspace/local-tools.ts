import { execFile } from 'node:child_process'
import { readdir, readFile } from 'node:fs/promises'
import { join, relative, resolve, sep } from 'node:path'
import { promisify } from 'node:util'
import { dialog, shell } from 'electron'
import type {
  ExternalAppKind,
  GitFileChange,
  GitWorkspaceSummary,
  WorkspaceFileContent,
  WorkspaceFileEntry
} from '@shared/ipc-contracts'

const execFileAsync = promisify(execFile)
const MAX_PREVIEW_BYTES = 256 * 1024
const IGNORED_DIRS = new Set(['.git', 'node_modules', '.venv', '__pycache__', 'dist', 'out'])

function resolveSafeWorkspacePath(workspaceDir: string, targetRelative?: string): string {
  const root = resolve(workspaceDir)
  if (!targetRelative) return root
  const resolved = resolve(root, targetRelative)
  if (resolved !== root && !resolved.startsWith(`${root}${sep}`)) {
    throw new Error(`Path escapes workspace root: ${targetRelative}`)
  }
  return resolved
}

async function runGit(cwd: string, args: string[]): Promise<string> {
  const { stdout } = await execFileAsync('git', args, {
    cwd,
    maxBuffer: 4 * 1024 * 1024
  })
  return stdout
}

export async function pickWorkspaceDirectory(): Promise<string | null> {
  const result = await dialog.showOpenDialog({
    properties: ['openDirectory', 'createDirectory'],
    title: '选择项目工作目录'
  })
  if (result.canceled || result.filePaths.length === 0) return null
  return result.filePaths[0]
}

export async function getGitSummary(workspaceDir: string): Promise<GitWorkspaceSummary> {
  const root = resolve(workspaceDir)
  try {
    const inside = (await runGit(root, ['rev-parse', '--is-inside-work-tree'])).trim()
    if (inside !== 'true') {
      return { isGitRepo: false, branch: null, files: [], totalAdditions: 0, totalDeletions: 0 }
    }
  } catch {
    return { isGitRepo: false, branch: null, files: [], totalAdditions: 0, totalDeletions: 0 }
  }

  let branch: string | null
  try {
    branch = (await runGit(root, ['rev-parse', '--abbrev-ref', 'HEAD'])).trim() || null
  } catch {
    branch = null
  }

  const statusMap = new Map<string, string>()
  try {
    const porcelain = await runGit(root, ['status', '--porcelain=v1', '-uall'])
    for (const rawLine of porcelain.split('\n')) {
      if (rawLine.length < 4) continue
      const code = rawLine.slice(0, 2).trim() || 'M'
      const rawPath = rawLine.slice(3).trim()
      const filePath = rawPath.includes(' -> ') ? rawPath.split(' -> ').pop()! : rawPath
      statusMap.set(filePath, code)
    }
  } catch {
    // Fallback to empty status
  }

  const statsMap = new Map<string, { additions: number; deletions: number }>()
  for (const diffArgs of [
    ['diff', '--numstat'],
    ['diff', '--cached', '--numstat']
  ]) {
    try {
      const out = await runGit(root, diffArgs)
      for (const line of out.split('\n')) {
        if (!line.trim()) continue
        const [addStr, delStr, filePath] = line.split('\t')
        if (!filePath) continue
        const prev = statsMap.get(filePath) ?? { additions: 0, deletions: 0 }
        const add = Number.parseInt(addStr, 10)
        const del = Number.parseInt(delStr, 10)
        statsMap.set(filePath, {
          additions: prev.additions + (Number.isNaN(add) ? 0 : add),
          deletions: prev.deletions + (Number.isNaN(del) ? 0 : del)
        })
      }
    } catch {
      // Ignore empty HEAD errors on unborn repos
    }
  }

  const allPaths = new Set([...statusMap.keys(), ...statsMap.keys()])
  const files: GitFileChange[] = []
  let totalAdditions = 0
  let totalDeletions = 0

  for (const filePath of Array.from(allPaths).sort()) {
    const stat = statsMap.get(filePath) ?? { additions: 0, deletions: 0 }
    totalAdditions += stat.additions
    totalDeletions += stat.deletions
    files.push({
      path: filePath,
      status: statusMap.get(filePath) ?? 'M',
      additions: stat.additions,
      deletions: stat.deletions
    })
  }

  return {
    isGitRepo: true,
    branch,
    files,
    totalAdditions,
    totalDeletions
  }
}

export async function getGitFileDiff(workspaceDir: string, filePath: string): Promise<string> {
  const root = resolve(workspaceDir)
  resolveSafeWorkspacePath(root, filePath)
  try {
    const unstaged = await runGit(root, ['diff', '--', filePath])
    const staged = await runGit(root, ['diff', '--cached', '--', filePath])
    const combined = [staged.trim(), unstaged.trim()].filter(Boolean).join('\n')
    if (combined) return combined
  } catch {
    // Fall through to untracked file preview
  }
  const full = await readWorkspaceFile(root, filePath)
  return full.content
    .split('\n')
    .map((line) => `+${line}`)
    .join('\n')
}

export async function listWorkspaceFiles(
  workspaceDir: string,
  relativePath?: string
): Promise<WorkspaceFileEntry[]> {
  const root = resolve(workspaceDir)
  const targetDir = resolveSafeWorkspacePath(root, relativePath)
  const dirents = await readdir(targetDir, { withFileTypes: true })
  const entries: WorkspaceFileEntry[] = []

  for (const entry of dirents) {
    if (entry.isDirectory() && IGNORED_DIRS.has(entry.name)) continue
    const fullPath = join(targetDir, entry.name)
    entries.push({
      name: entry.name,
      relativePath: relative(root, fullPath).split(sep).join('/'),
      isDirectory: entry.isDirectory()
    })
  }

  return entries.sort((a, b) => {
    if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}

export async function readWorkspaceFile(
  workspaceDir: string,
  relativePath: string
): Promise<WorkspaceFileContent> {
  const targetPath = resolveSafeWorkspacePath(workspaceDir, relativePath)
  const buf = await readFile(targetPath)
  const truncated = buf.byteLength > MAX_PREVIEW_BYTES
  const slice = truncated ? buf.subarray(0, MAX_PREVIEW_BYTES) : buf
  return {
    content: slice.toString('utf-8'),
    truncated
  }
}

export async function openInExternalApp(
  workspaceDir: string,
  target: ExternalAppKind
): Promise<void> {
  const root = resolve(workspaceDir)
  if (target === 'finder') {
    await shell.openPath(root)
    return
  }

  if (process.platform === 'darwin') {
    const appNameMap: Record<Exclude<ExternalAppKind, 'finder'>, string> = {
      terminal: 'Terminal',
      vscode: 'Visual Studio Code',
      cursor: 'Cursor',
      idea: 'IntelliJ IDEA'
    }
    await execFileAsync('open', ['-a', appNameMap[target], root])
    return
  }

  const cliMap: Record<Exclude<ExternalAppKind, 'finder'>, string> = {
    terminal: process.platform === 'win32' ? 'cmd' : 'x-terminal-emulator',
    vscode: 'code',
    cursor: 'cursor',
    idea: 'idea'
  }
  await execFileAsync(cliMap[target], [root])
}
