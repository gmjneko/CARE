import { ChildProcess, execFile, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import net from 'node:net'
import type { RuntimeConnectionInfo, SidecarStatus } from '@shared/ipc-contracts'
import { resolveSidecarLaunchSpec } from './runtime-locator'

const MAX_LOG_LINES = 500
const HEALTH_POLL_INTERVAL_MS = 120
const HEALTH_TIMEOUT_MS = 25_000
const STOP_GRACE_MS = 5_000

async function findFreeLoopbackPort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address()
      if (!addr || typeof addr === 'string') {
        server.close()
        reject(new Error('Failed to allocate loopback port'))
        return
      }
      const { port } = addr
      server.close((closeErr) => {
        if (closeErr) reject(closeErr)
        else resolvePort(port)
      })
    })
  })
}

function buildCorsOrigins(): string {
  const origins = new Set<string>(['http://localhost:5173', 'http://127.0.0.1:5173', 'null'])
  const devUrl = process.env.ELECTRON_RENDERER_URL?.trim()
  if (devUrl) {
    try {
      origins.add(new URL(devUrl).origin)
    } catch {
      // Ignore malformed dev URL
    }
  }
  return Array.from(origins).join(',')
}

export class SidecarManager {
  private child: ChildProcess | null = null
  private status: SidecarStatus = 'stopped'
  private baseUrl = ''
  private authToken: string | null = null
  private mode: 'managed' | 'attached' = 'managed'
  private lastError: string | null = null
  private readonly logs: string[] = []
  private startPromise: Promise<RuntimeConnectionInfo> | null = null
  private stopping = false

  public getConnectionSnapshot(): RuntimeConnectionInfo {
    return {
      baseUrl: this.baseUrl,
      authToken: this.authToken,
      status: this.status,
      mode: this.mode,
      error: this.lastError
    }
  }

  public getLogs(): string[] {
    return [...this.logs]
  }

  public async ensureStarted(): Promise<RuntimeConnectionInfo> {
    if (this.status === 'ready' || this.status === 'attached') {
      return this.getConnectionSnapshot()
    }
    if (this.startPromise) {
      return this.startPromise
    }
    this.startPromise = this.startInternal().finally(() => {
      this.startPromise = null
    })
    return this.startPromise
  }

  public async restart(): Promise<RuntimeConnectionInfo> {
    await this.stop()
    return this.ensureStarted()
  }

  public async stop(): Promise<void> {
    this.stopping = true
    const proc = this.child
    this.child = null
    if (!proc || proc.exitCode !== null || !proc.pid) {
      this.status = 'stopped'
      this.stopping = false
      return
    }

    const pid = proc.pid
    this.appendLog(`[sidecar] Stopping process tree (pid=${pid})...`)

    const exited = new Promise<void>((resolveExit) => {
      proc.once('exit', () => resolveExit())
    })

    try {
      if (process.platform === 'win32') {
        execFile('taskkill', ['/PID', String(pid), '/T', '/F'], () => undefined)
      } else {
        process.kill(-pid, 'SIGTERM')
      }
    } catch {
      // Process may already have exited
    }

    const timeout = new Promise<void>((resolveTimeout) => {
      setTimeout(() => {
        if (proc.exitCode === null && process.platform !== 'win32') {
          try {
            process.kill(-pid, 'SIGKILL')
          } catch {
            // Ignore ESRCH
          }
        }
        resolveTimeout()
      }, STOP_GRACE_MS)
    })

    await Promise.race([exited, timeout])
    this.status = 'stopped'
    this.stopping = false
  }

  private async startInternal(): Promise<RuntimeConnectionInfo> {
    const externalUrl = process.env.CARE_RUNTIME_URL?.trim()
    if (externalUrl) {
      this.mode = 'attached'
      this.baseUrl = externalUrl.replace(/\/+$/, '')
      this.authToken = process.env.RUNTIME_AUTH_TOKEN?.trim() || null
      this.status = 'starting'
      this.lastError = null
      this.appendLog(`[sidecar] Attaching to external runtime at ${this.baseUrl}`)
      try {
        await this.waitForHealth(this.baseUrl)
        this.status = 'attached'
        this.appendLog('[sidecar] External runtime is healthy.')
      } catch (err) {
        this.status = 'error'
        this.lastError = err instanceof Error ? err.message : String(err)
        this.appendLog(`[sidecar] External runtime health check failed: ${this.lastError}`)
      }
      return this.getConnectionSnapshot()
    }

    this.mode = 'managed'
    this.status = 'starting'
    this.lastError = null

    try {
      const port = await findFreeLoopbackPort()
      const token = randomBytes(32).toString('hex')
      const spec = resolveSidecarLaunchSpec(port)

      this.baseUrl = `http://127.0.0.1:${port}`
      this.authToken = token

      this.appendLog(`[sidecar] Launching ${spec.command} ${spec.args.join(' ')} (cwd=${spec.cwd})`)
      const proc = spawn(spec.command, spec.args, {
        cwd: spec.cwd,
        detached: process.platform !== 'win32',
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          ...process.env,
          MODE: 'local',
          RUNTIME_AUTH_TOKEN: token,
          CARE_CORS_ORIGINS: buildCorsOrigins(),
          PYTHONUNBUFFERED: '1'
        }
      })

      this.child = proc
      proc.stdout?.on('data', (chunk: Buffer) => this.appendChunk('stdout', chunk))
      proc.stderr?.on('data', (chunk: Buffer) => this.appendChunk('stderr', chunk))

      const earlyExit = new Promise<never>((_, reject) => {
        proc.once('error', (err) => {
          reject(new Error(`Failed to spawn agent-runtime: ${err.message}`))
        })
        proc.once('exit', (code, signal) => {
          if (!this.stopping) {
            const msg = `agent-runtime exited unexpectedly (code=${code}, signal=${signal})`
            this.appendLog(`[sidecar] ${msg}`)
            this.status = 'error'
            this.lastError = msg
            reject(new Error(msg))
          }
        })
      })

      await Promise.race([this.waitForHealth(this.baseUrl), earlyExit])
      this.status = 'ready'
      this.appendLog(`[sidecar] Ready at ${this.baseUrl}`)
    } catch (err) {
      this.status = 'error'
      this.lastError = err instanceof Error ? err.message : String(err)
      this.appendLog(`[sidecar] Startup error: ${this.lastError}`)
      await this.stop()
      this.status = 'error'
    }

    return this.getConnectionSnapshot()
  }

  private async waitForHealth(baseUrl: string): Promise<void> {
    const deadline = Date.now() + HEALTH_TIMEOUT_MS
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`${baseUrl}/healthz`, { signal: AbortSignal.timeout(1000) })
        if (res.ok) return
      } catch {
        // Retry until ready
      }
      await new Promise((r) => setTimeout(r, HEALTH_POLL_INTERVAL_MS))
    }
    throw new Error(`Timed out waiting for agent-runtime at ${baseUrl}/healthz`)
  }

  private appendChunk(stream: 'stdout' | 'stderr', chunk: Buffer): void {
    const text = chunk.toString('utf-8')
    for (const line of text.split(/\r?\n/)) {
      if (line.trim()) {
        this.appendLog(`[${stream}] ${line}`)
      }
    }
  }

  private appendLog(line: string): void {
    this.logs.push(`${new Date().toISOString()} ${line}`)
    if (this.logs.length > MAX_LOG_LINES) {
      this.logs.splice(0, this.logs.length - MAX_LOG_LINES)
    }
  }
}
