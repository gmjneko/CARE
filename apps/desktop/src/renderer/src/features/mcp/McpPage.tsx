import { CheckCircle2, Cpu, Server } from 'lucide-react'
import { useRuntimeStore } from '@renderer/stores/runtime-store'

export function McpPage(): React.JSX.Element {
  const adapters = useRuntimeStore((s) => s.adapters)

  return (
    <div className="flex-1 overflow-y-auto px-8 py-7">
      <div className="mx-auto max-w-[820px] space-y-6">
        <div>
          <h1 className="text-heading font-semibold text-[var(--text-primary)]">
            MCP 服务与底座能力
          </h1>
          <p className="mt-1 text-ui text-[var(--text-secondary)]">
            支持本地 `stdio` 与企业远程 `Streamable HTTP` MCP
            Server（密钥走子进程环境变量，绝不落盘明文）。Phase 1.5
            运行时扩展落地后可在此统一增删启停。
          </p>
        </div>

        <div className="rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-5">
          <div className="flex items-center gap-2 text-ui font-semibold text-[var(--text-primary)]">
            <Server className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.7} />
            <span>已配置 MCP Servers</span>
          </div>
          <div className="mt-4 rounded-xl border border-dashed border-[var(--border-card)] bg-[var(--bg-subtle)]/40 py-8 text-center text-ui text-[var(--text-muted)]">
            暂未配置自定义 MCP Server（等待对接 `~/.care/settings.json` 的 `mcp_servers` 契约）
          </div>
        </div>

        <div className="rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] p-5">
          <div className="flex items-center gap-2 text-ui font-semibold text-[var(--text-primary)]">
            <Cpu className="h-4 w-4 text-[var(--text-secondary)]" strokeWidth={1.7} />
            <span>已注册 Agent 底座能力矩阵（GET /adapters）</span>
          </div>
          <div className="mt-3 space-y-2.5">
            {adapters.length === 0 ? (
              <div className="text-ui text-[var(--text-muted)]">尚未获取到已注册底座信息</div>
            ) : (
              adapters.map((adapter) => (
                <div
                  key={adapter.kind}
                  className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/40 p-3.5"
                >
                  <div className="font-mono text-code font-semibold text-[var(--text-primary)]">
                    {adapter.kind}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {Object.entries(adapter.capabilities).map(([cap, enabled]) => (
                      <span
                        key={cap}
                        className="inline-flex items-center gap-1 rounded-full border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2.5 py-0.5 text-caption text-[var(--text-secondary)]"
                      >
                        <CheckCircle2
                          className={
                            enabled
                              ? 'h-3.5 w-3.5 text-[var(--status-success)]'
                              : 'h-3.5 w-3.5 text-[var(--text-muted)]/40'
                          }
                        />
                        <span>{cap}</span>
                      </span>
                    ))}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
