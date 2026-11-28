import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { app } from 'electron'

export interface RuntimeLaunchSpec {
  command: string
  args: string[]
  cwd: string
}

/**
 * Locate the `services/agent-runtime` directory by walking upward from candidate roots.
 */
function findAgentRuntimeDir(): string {
  const candidates = [process.cwd(), app.getAppPath(), __dirname]
  for (const start of candidates) {
    let current = resolve(start)
    for (let depth = 0; depth < 8; depth += 1) {
      const target = join(current, 'services', 'agent-runtime')
      if (existsSync(join(target, 'pyproject.toml'))) {
        return target
      }
      const parent = dirname(current)
      if (parent === current) break
      current = parent
    }
  }
  throw new Error(
    'Unable to locate services/agent-runtime directory. Set CARE_RUNTIME_URL to connect to an external runtime.'
  )
}

/**
 * Resolve the executable command and arguments for spawning the local Python Sidecar.
 */
export function resolveSidecarLaunchSpec(port: number): RuntimeLaunchSpec {
  const overrideBin = process.env.CARE_RUNTIME_BIN?.trim()
  if (overrideBin) {
    return {
      command: overrideBin,
      args: ['--mode', 'local', '--host', '127.0.0.1', '--port', String(port)],
      cwd: app.getPath('home')
    }
  }

  const runtimeDir = findAgentRuntimeDir()
  const uvBin = process.env.UV_BIN?.trim() || 'uv'
  return {
    command: uvBin,
    args: ['run', 'care-runtime', '--mode', 'local', '--host', '127.0.0.1', '--port', String(port)],
    cwd: runtimeDir
  }
}
