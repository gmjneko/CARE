import { useEffect, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Check, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import type {
  ApiProtocol,
  LocalSettingsResponse,
  LocalSettingsUpdate,
  ModelProfileInput,
  ThinkingLevel
} from '@renderer/sdk/runtime-types'
import { useRuntimeStore } from '@renderer/stores/runtime-store'
import { useWorkbenchStore } from '@renderer/stores/workbench-store'

interface EditableProfile {
  name: string
  model: string
  provider: string
  baseUrl: string
  apiProtocol: ApiProtocol
  thinkingLevel: ThinkingLevel | ''
  hasApiKey: boolean
  apiKeyAction: 'keep' | 'clear' | 'set'
  apiKeyInput: string
}

const API_PROTOCOLS: ApiProtocol[] = [
  'openai-completions',
  'openai-responses',
  'anthropic-messages',
  'google-generative-ai'
]

function toEditableProfiles(settings: LocalSettingsResponse | null): EditableProfile[] {
  if (!settings) return []
  return Object.entries(settings.profiles).map(([name, prof]) => ({
    name,
    model: prof.model,
    provider: prof.provider ?? '',
    baseUrl: prof.base_url ?? '',
    apiProtocol: prof.api_protocol,
    thinkingLevel: prof.thinking_level ?? '',
    hasApiKey: prof.has_api_key,
    apiKeyAction: 'keep',
    apiKeyInput: ''
  }))
}

export function SettingsDialog(): React.JSX.Element {
  const open = useWorkbenchStore((s) => s.settingsOpen)
  const setOpen = useWorkbenchStore((s) => s.setSettingsOpen)
  const settings = useRuntimeStore((s) => s.settings)

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/20 backdrop-blur-[1px]" />
        {open && <SettingsDialogBody initialSettings={settings} />}
      </Dialog.Portal>
    </Dialog.Root>
  )
}

function SettingsDialogBody({
  initialSettings
}: {
  initialSettings: LocalSettingsResponse | null
}): React.JSX.Element {
  const connection = useRuntimeStore((s) => s.connection)
  const saveSettings = useRuntimeStore((s) => s.saveSettings)
  const restartSidecar = useRuntimeStore((s) => s.restartSidecar)

  const [tab, setTab] = useState<'models' | 'runtime'>('models')
  const [profiles, setProfiles] = useState<EditableProfile[]>(() =>
    toEditableProfiles(initialSettings)
  )
  const [activeProfile, setActiveProfile] = useState<string>(
    () => initialSettings?.active_profile ?? ''
  )
  const [piBin, setPiBin] = useState<string>(() => initialSettings?.adapter_bins.pi ?? '')
  const [logs, setLogs] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  useEffect(() => {
    let cancelled = false
    void window.careDesktop
      .getSidecarLogs()
      .then((lines) => {
        if (!cancelled) setLogs(lines)
      })
      .catch(() => {
        if (!cancelled) setLogs([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleAddProfile = (): void => {
    const idx = profiles.length + 1
    setProfiles((prev) => [
      ...prev,
      {
        name: `profile-${idx}`,
        model: '',
        provider: '',
        baseUrl: '',
        apiProtocol: 'openai-completions',
        thinkingLevel: '',
        hasApiKey: false,
        apiKeyAction: 'keep',
        apiKeyInput: ''
      }
    ])
  }

  const updateProfile = (index: number, patch: Partial<EditableProfile>): void => {
    setProfiles((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  }

  const removeProfile = (index: number): void => {
    setProfiles((prev) => {
      const removed = prev[index]
      const next = prev.filter((_, i) => i !== index)
      if (removed && activeProfile === removed.name) {
        setActiveProfile(next[0]?.name ?? '')
      }
      return next
    })
  }

  const handleSave = async (): Promise<void> => {
    setSaving(true)
    setFeedback(null)
    try {
      const profileMap: Record<string, ModelProfileInput> = {}
      for (const item of profiles) {
        const cleanName = item.name.trim()
        if (!cleanName) throw new Error('Profile 名称不能为空')
        const entry: ModelProfileInput = {
          model: item.model.trim(),
          provider: item.provider.trim() || null,
          base_url: item.baseUrl.trim() || null,
          api_protocol: item.apiProtocol,
          thinking_level: item.thinkingLevel || null
        }
        if (item.apiKeyAction === 'set' && item.apiKeyInput.trim()) {
          entry.api_key = item.apiKeyInput.trim()
        } else if (item.apiKeyAction === 'clear') {
          entry.api_key = null
        }
        profileMap[cleanName] = entry
      }

      const payload: LocalSettingsUpdate = {
        active_profile: activeProfile.trim() || null,
        profiles: profileMap,
        adapter_bins: piBin.trim() ? { pi: piBin.trim() } : {}
      }
      const saved = await saveSettings(payload)
      setProfiles(toEditableProfiles(saved))
      setActiveProfile(saved.active_profile ?? '')
      setFeedback({ kind: 'ok', text: '设置已保存到 ~/.care/settings.json' })
    } catch (err) {
      setFeedback({
        kind: 'err',
        text: err instanceof Error ? err.message : String(err)
      })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[82vh] w-[680px] max-w-[92vw] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-[var(--border-card)] bg-[var(--bg-surface)] shadow-[var(--shadow-floating)]">
      <div className="flex items-center justify-between border-b border-[var(--border-hairline)] px-5 py-3.5">
        <div className="flex items-center gap-4">
          <Dialog.Title className="text-heading font-semibold text-[var(--text-primary)]">
            工作台设置
          </Dialog.Title>
          <div className="flex items-center gap-1 rounded-lg bg-[var(--bg-subtle)] p-0.5 text-ui">
            <button
              type="button"
              onClick={() => setTab('models')}
              className={
                tab === 'models'
                  ? 'rounded-md bg-[var(--bg-surface)] px-2.5 py-1 font-medium text-[var(--text-primary)] shadow-2xs'
                  : 'px-2.5 py-1 text-[var(--text-secondary)]'
              }
            >
              模型与密钥
            </button>
            <button
              type="button"
              onClick={() => setTab('runtime')}
              className={
                tab === 'runtime'
                  ? 'rounded-md bg-[var(--bg-surface)] px-2.5 py-1 font-medium text-[var(--text-primary)] shadow-2xs'
                  : 'px-2.5 py-1 text-[var(--text-secondary)]'
              }
            >
              本地运行时
            </button>
          </div>
        </div>
        <Dialog.Close className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]">
          <X className="h-4 w-4" />
        </Dialog.Close>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {tab === 'models' ? (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="text-caption text-[var(--text-secondary)]">
                已保存的 API Key 仅存在本机 `~/.care/settings.json`，接口绝不回传明文。
              </div>
              <button
                type="button"
                onClick={handleAddProfile}
                className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1 text-ui font-medium text-[var(--text-primary)] hover:bg-[var(--bg-hover)]"
              >
                <Plus className="h-3.5 w-3.5" />
                新增 Profile
              </button>
            </div>

            {profiles.length === 0 ? (
              <div className="rounded-xl border border-dashed border-[var(--border-card)] py-8 text-center text-ui text-[var(--text-muted)]">
                暂无自定义模型配置（将沿用底层 `pi` 的默认模型）
              </div>
            ) : (
              <div className="space-y-3">
                {profiles.map((prof, idx) => (
                  <div
                    key={idx}
                    className="space-y-2.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/40 p-3.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <label className="inline-flex items-center gap-2 text-ui font-medium text-[var(--text-primary)]">
                        <input
                          type="radio"
                          name="active_profile"
                          checked={activeProfile === prof.name}
                          onChange={() => setActiveProfile(prof.name)}
                        />
                        <span>设为默认</span>
                      </label>
                      <button
                        type="button"
                        onClick={() => removeProfile(idx)}
                        className="inline-flex items-center gap-1 text-caption text-[var(--text-muted)] hover:text-[var(--status-danger)]"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        删除
                      </button>
                    </div>

                    <div className="grid grid-cols-2 gap-2.5">
                      <Field label="Profile 名称">
                        <input
                          type="text"
                          value={prof.name}
                          onChange={(e) => {
                            const oldName = prof.name
                            updateProfile(idx, { name: e.target.value })
                            if (activeProfile === oldName) setActiveProfile(e.target.value)
                          }}
                          className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-ui focus:outline-none"
                        />
                      </Field>
                      <Field label="Model ID">
                        <input
                          type="text"
                          placeholder="如 deepseek-chat / claude-sonnet-4-5"
                          value={prof.model}
                          onChange={(e) => updateProfile(idx, { model: e.target.value })}
                          className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-ui focus:outline-none"
                        />
                      </Field>
                      <Field label="Base URL（可选）">
                        <input
                          type="text"
                          placeholder="https://api.example.com/v1"
                          value={prof.baseUrl}
                          onChange={(e) => updateProfile(idx, { baseUrl: e.target.value })}
                          className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-ui focus:outline-none"
                        />
                      </Field>
                      <Field label="API 协议">
                        <select
                          value={prof.apiProtocol}
                          onChange={(e) =>
                            updateProfile(idx, { apiProtocol: e.target.value as ApiProtocol })
                          }
                          className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-ui focus:outline-none"
                        >
                          {API_PROTOCOLS.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="Provider（无 Base URL 时必填）">
                        <input
                          type="text"
                          placeholder="可选，如 openai / anthropic"
                          value={prof.provider}
                          onChange={(e) => updateProfile(idx, { provider: e.target.value })}
                          className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-ui focus:outline-none"
                        />
                      </Field>
                      <Field
                        label={
                          prof.hasApiKey && prof.apiKeyAction === 'keep'
                            ? 'API Key（已保存，留空保持不变）'
                            : prof.apiKeyAction === 'clear'
                              ? 'API Key（保存时将清除）'
                              : 'API Key（可选）'
                        }
                      >
                        <div className="flex gap-1.5">
                          <input
                            type="password"
                            placeholder={prof.hasApiKey ? '••••••••••••' : 'sk-...'}
                            value={prof.apiKeyInput}
                            onChange={(e) =>
                              updateProfile(idx, {
                                apiKeyInput: e.target.value,
                                apiKeyAction: e.target.value ? 'set' : 'keep'
                              })
                            }
                            className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-ui focus:outline-none"
                          />
                          {prof.hasApiKey && (
                            <button
                              type="button"
                              onClick={() =>
                                updateProfile(idx, {
                                  apiKeyInput: '',
                                  apiKeyAction: prof.apiKeyAction === 'clear' ? 'keep' : 'clear'
                                })
                              }
                              className="shrink-0 rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 text-caption text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]"
                            >
                              {prof.apiKeyAction === 'clear' ? '撤销清除' : '清除'}
                            </button>
                          )}
                        </div>
                      </Field>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div className="border-t border-[var(--border-hairline)] pt-3">
              <Field label="Pi 可执行文件路径覆盖（可选，adapter_bins.pi）">
                <input
                  type="text"
                  placeholder="留空自动解析 PATH 或 ~/.pi/agent/bin/pi"
                  value={piBin}
                  onChange={(e) => setPiBin(e.target.value)}
                  className="w-full rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-2.5 py-1.5 font-mono text-code focus:outline-none"
                />
              </Field>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/50 p-3.5 text-ui">
              <div className="space-y-1">
                <div className="font-medium text-[var(--text-primary)]">
                  状态：{connection?.status ?? 'unknown'}（{connection?.mode ?? 'managed'}）
                </div>
                <div className="font-mono text-code text-[var(--text-secondary)]">
                  {connection?.baseUrl || '未连接'}
                </div>
              </div>
              <button
                type="button"
                onClick={() => void restartSidecar()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-card)] bg-[var(--bg-surface)] px-3 py-1.5 font-medium text-[var(--text-primary)] hover:bg-[var(--bg-hover)]"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                重启 Sidecar
              </button>
            </div>

            <div>
              <div className="mb-1.5 text-ui font-medium text-[var(--text-secondary)]">
                Sidecar 诊断日志（最近 500 行）
              </div>
              <pre className="max-h-[300px] overflow-auto rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/60 p-3 font-mono text-code text-[var(--text-secondary)]">
                {logs.length > 0 ? logs.join('\n') : '暂无日志输出'}
              </pre>
            </div>
          </div>
        )}
      </div>

      {tab === 'models' && (
        <div className="flex items-center justify-between border-t border-[var(--border-hairline)] px-5 py-3">
          <div className="text-ui">
            {feedback && (
              <span
                className={
                  feedback.kind === 'ok'
                    ? 'text-[var(--status-success)]'
                    : 'text-[var(--status-danger)]'
                }
              >
                {feedback.text}
              </span>
            )}
          </div>
          <button
            type="button"
            disabled={saving}
            onClick={() => void handleSave()}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[var(--accent-primary)] px-4 text-ui font-medium text-[var(--accent-primary-fg)] hover:opacity-90 disabled:opacity-50"
          >
            <Check className="h-3.5 w-3.5" />
            <span>{saving ? '保存中...' : '保存配置'}</span>
          </button>
        </div>
      )}
    </Dialog.Content>
  )
}

function Field({
  label,
  children
}: {
  label: string
  children: React.ReactNode
}): React.JSX.Element {
  return (
    <div className="space-y-1">
      <div className="text-caption font-medium text-[var(--text-secondary)]">{label}</div>
      {children}
    </div>
  )
}
