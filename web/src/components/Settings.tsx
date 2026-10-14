import { useEffect, useState } from 'react'
import { Check, Loader2, RotateCcw } from 'lucide-react'
import { Agent, api, Directory, Role, ROLE_META, RuntimeCfg } from '../api'
import { Avatar } from './Avatar'
import { RuntimeBadge } from './RuntimeBadge'

type Models = Record<RuntimeCfg['backend'], string[]>
type Provider = { id: string; models: string[]; model_backends: Record<string, 'claude' | 'codex'> }
const EFFORTS = ['', 'low', 'medium', 'high', 'xhigh', 'max']

export function Settings({ dir }: { dir: Directory }) {
  const [overrides, setOverrides] = useState<Record<string, RuntimeCfg> | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [models, setModels] = useState<Models>({ claude: [], codex: [], galaxy_api: [] })
  const [oldDefaults, setOldDefaults] = useState<Record<string, RuntimeCfg>>({})
  const load = () => api<{ overrides: Record<string, RuntimeCfg>; models: Models; providers?: Provider[]; old_one_defaults?: Record<string, RuntimeCfg> }>('runtimes')
    .then((r) => { setOverrides(r.overrides); setModels(r.models); setProviders(r.providers ?? []); setOldDefaults(r.old_one_defaults ?? {}) })
  useEffect(() => { load() }, [])

  const roles: Role[] = ['azathoth', 'nyarlathotep']
  const gods = roles.map((r) => dir.agents.find((a) => a.role === r)).filter(Boolean) as Agent[]
  const olds = dir.agents.filter((a) => a.role === 'old_one' && a.desired === 'running')

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-6 px-4 py-6 sm:px-8 sm:py-8">
        <div>
          <h2 className="text-lg font-semibold text-white">模型与后端</h2>
          <p className="mt-1 text-sm text-slate-400">
            修改后在该 agent 当前回合结束时生效。不同后端使用独立会话，程序会把永久上下文和最近的交流记录带过去。
            星系 API 在尚未执行工具时可自动回退 30 分钟；已执行工具的失败回合不会自动重放。
          </p>
        </div>
        {!overrides ? <div className="flex justify-center py-16"><Loader2 className="h-5 w-5 animate-spin text-slate-600" /></div> : (
          <>
            {gods.map((a) => (
              <RuntimeEditor key={a.id} agent={a} keyName={a.role} override={overrides[a.role]} models={models} providers={providers} onSaved={load} />
            ))}
            <GalaxyAI />
            <h3 className="pt-4 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">各星系旧日支配者默认（新建或未单独设置的旧日支配者用这里）</h3>
            {dir.galaxies.filter((g) => g.hub).map((g) => {
              const key = `galaxy:${g.id}`
              // A stand-in "agent" so the same editor shows the galaxy default.
              const stand = { id: key, name: g.name, role: 'old_one', runtime: { config: oldDefaults[key] } } as unknown as Agent
              return <RuntimeEditor key={key} agent={stand} keyName={key} override={overrides[key]} models={models} providers={providers}
                onSaved={load} isDefault />
            })}
            {olds.length > 0 && <h3 className="pt-4 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">旧日支配者（逐个设置；未设置则用旧日支配者默认）</h3>}
            {olds.map((a) => (
              <RuntimeEditor key={a.id} agent={a} keyName={a.id} override={overrides[a.id]} models={models} providers={providers} onSaved={load} />
            ))}
          </>
        )}
      </div>
    </div>
  )
}

type GalaxyAIRow = { id: string; name: string; config: { provider?: string; model?: string; anthropic?: { base_url: string } } | null
                     source: 'owner' | 'config' | 'default' | 'none'; choices: { provider: string; model: string }[] }

/** Each hub's judgment model (probe review, progress summary, reuse choice, handover). Claude models offered to that galaxy only. */
function GalaxyAI() {
  const [rows, setRows] = useState<GalaxyAIRow[] | null>(null)
  const [error, setError] = useState('')
  const load = () => api<GalaxyAIRow[]>('galaxy_ai').then(setRows).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])
  const save = async (galaxy: string, value: string) => {
    setError('')
    const [provider, model] = value ? value.split('|') : []
    try { setRows(await api<GalaxyAIRow[]>('galaxy_ai', { galaxy, config: value ? { provider, model } : null })) }
    catch (e) { setError((e as Error).message) }
  }
  const label = (c: GalaxyAIRow['config']) => !c ? '未配置（AI 判断全部走程序兜底）'
    : c.anthropic ? `${c.model} · 直连 ${c.anthropic.base_url}` : `${c.model} · 经 ${c.provider} 网关`
  return (
    <section className="rounded-2xl border border-white/[0.07] bg-white/[0.03] p-5">
      <p className="font-medium text-white">星系枢纽 AI</p>
      <p className="mt-1 text-xs text-slate-500">枢纽用它审核探针、写检查点总结、判断复用、写交接摘要。不可用时程序兜底，任务记录里会显示「AI 总结不可用」。</p>
      {!rows ? <Loader2 className="mt-4 h-4 w-4 animate-spin text-slate-600" /> : rows.length === 0
        ? <p className="mt-3 text-sm text-slate-500">没有在线的 v3 星系。</p>
        : <div className="mt-4 space-y-3">{rows.map((r) => (
          <div key={r.id} className="flex flex-wrap items-center gap-3 text-sm">
            <span className="w-28 shrink-0 text-slate-300">{r.name}</span>
            <select value={r.source === 'owner' && r.config ? `${r.config.provider}|${r.config.model}` : ''}
              onChange={(e) => save(r.id, e.target.value)}
              className="min-w-0 flex-1 rounded-lg border border-white/10 bg-abyss-800 p-2 text-sm">
              <option value="">默认：{label(r.source === 'owner' ? null : r.config)}</option>
              {r.choices.map((c) => <option key={`${c.provider}|${c.model}`} value={`${c.provider}|${c.model}`}>{c.model} · 经 {c.provider} 网关</option>)}
            </select>
          </div>))}</div>}
      {error && <p role="alert" className="mt-2 text-xs text-rose-300">{error}</p>}
    </section>
  )
}

function RuntimeEditor({ agent, keyName, override, models, providers, onSaved, isDefault }: {
  agent: Agent; keyName: string; override?: RuntimeCfg; models: Models; providers: Provider[]; onSaved: () => void; isDefault?: boolean
}) {
  // Without an override, start from what the agent actually runs (temple default for old_ones), not the galaxy's local fallback.
  const effective = () => override ?? agent.runtime?.config ?? agent.runtime?.default ?? { backend: 'codex', model: '' }
  const base = effective()
  const [cfg, setCfg] = useState<RuntimeCfg>(base)
  const [fb, setFb] = useState<boolean>(!!base.fallback)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { const b = effective(); setCfg(b); setFb(!!b.fallback) }, [override, agent.runtime?.config?.model, agent.runtime?.config?.backend]) // eslint-disable-line
  const m = ROLE_META[agent.role]

  const save = async (reset = false) => {
    setBusy(true); setError('')
    try {
      const config = reset ? null : { ...cfg, fallback: fb ? cfg.fallback ?? { backend: cfg.backend === 'claude' ? 'codex' : 'claude', model: '' } : null }
      await api('runtimes', { key: keyName, config })
      setSaved(true); setTimeout(() => setSaved(false), 1500)
      onSaved()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="relative overflow-hidden rounded-2xl border border-white/[0.07] bg-white/[0.03] p-5">
      <div className={`absolute inset-x-0 top-0 h-px bg-gradient-to-r ${m.grad} opacity-60`} />
      <header className="mb-4 flex flex-wrap items-center gap-3">
        <Avatar role={agent.role} live={agent} />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-white">{agent.name}</p>
          <p className="text-xs text-slate-500">{isDefault ? (override ? '本星系已自定义' : '沿用全局旧日支配者默认')
            : override ? '已自定义' : agent.role === 'old_one' ? '沿用本星系默认' : '默认'}</p>
        </div>
        {!isDefault && <div className="flex items-center gap-2 text-xs text-slate-500">当前 <RuntimeBadge runtime={agent.runtime} /></div>}
        {agent.runtime?.pending && <p className="w-full text-xs text-amber-300">
          已保存，待当前回合结束后切换到 {agent.runtime.pending.model}（正在进行的回合不会被打断）</p>}
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        <Group title="主后端">
          <BackendFields cfg={cfg} models={models} providers={providers} onChange={setCfg} />
        </Group>
        <Group title={<label className="flex cursor-pointer items-center gap-2"><input type="checkbox" checked={fb} onChange={(e) => setFb(e.target.checked)} className="accent-violet-500" />后备（主后端出错时）</label>}>
          {fb ? (
            <BackendFields cfg={cfg.fallback ?? { backend: cfg.backend === 'claude' ? 'codex' : 'claude', model: '' }} models={models} providers={providers}
              onChange={(f) => setCfg({ ...cfg, fallback: f })} />
          ) : <p className="text-sm text-slate-500">未启用。主后端出错时该回合直接失败。</p>}
        </Group>
      </div>

      {['azathoth', 'nyarlathotep'].includes(agent.role) && cfg.backend === 'claude' && (
        <Group title="记忆提炼（在主会话分叉上运行，共享缓存）" className="mt-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="模型">
              <Select value={cfg.memory_model ?? ''} options={withValue(['', ...models.claude], cfg.memory_model)} onChange={(v) => setCfg({ ...cfg, memory_model: v || undefined })} />
            </Field>
            <Field label="effort">
              <Select value={cfg.memory_effort ?? ''} options={EFFORTS} onChange={(v) => setCfg({ ...cfg, memory_effort: v || undefined })} />
            </Field>
          </div>
        </Group>
      )}

      <footer className="mt-5 flex items-center justify-end gap-2">
        {error && <p className="mr-auto text-xs text-rose-400">{error}</p>}
        {override && (
          <button onClick={() => save(true)} disabled={busy} className="flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm text-slate-400 hover:bg-white/5 hover:text-white">
            <RotateCcw className="h-4 w-4" />恢复默认
          </button>
        )}
        <button onClick={() => save()} disabled={busy || !cfg.model || (fb && !cfg.fallback?.model)}
          className={`flex items-center gap-2 rounded-xl bg-gradient-to-r ${m.grad} px-4 py-2 text-sm font-medium text-white shadow-lg transition hover:brightness-110 disabled:opacity-40`}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : null}
          {saved ? '已保存' : '保存'}
        </button>
      </footer>
    </section>
  )
}

function BackendFields({ cfg, models, providers, onChange }: { cfg: RuntimeCfg; models: Models; providers: Provider[]; onChange: (c: RuntimeCfg) => void }) {
  const source = cfg.provider ? 'galaxy_api' : cfg.backend
  const provider = providers.find(p => p.id === cfg.provider)
  const selectAPIModel = (p: Provider | undefined, model: string) => {
    const backend = p?.model_backends[model] === 'claude' ? 'claude' : 'galaxy_api'
    onChange({ ...cfg, provider: p?.id ?? cfg.provider, backend, model,
      service_tier: backend === 'claude' ? undefined : cfg.service_tier,
      effort: model === 'gpt-6-astra' && !cfg.effort ? 'medium' : cfg.effort })
  }
  return (
    <div className="space-y-3">
      <div className="flex gap-1 rounded-xl bg-black/30 p-1">
        {(['claude', 'codex', 'galaxy_api'] as const).filter((b) => models[b]).map((b) => (
          <button key={b} aria-pressed={source === b} onClick={() => {
            if (b === source) return
            if (b === 'galaxy_api') selectAPIModel(providers[0], providers[0]?.models[0] ?? '')
            else onChange({ ...cfg, backend: b, model: models[b]?.[0] ?? '', service_tier: undefined, provider: undefined })
          }}
            className={`flex-1 rounded-lg py-1.5 text-sm transition ${source === b
              ? b === 'claude' ? 'bg-orange-400/20 text-orange-100' : 'bg-sky-400/20 text-sky-100'
              : 'text-slate-500 hover:text-slate-300'}`}>
            {b === 'claude' ? 'Claude Code' : b === 'galaxy_api' ? '星系 API' : '个人 Codex'}
          </button>
        ))}
      </div>
      {source === 'galaxy_api' && (
        <Field label="在线 API 星系">
          <Select value={cfg.provider ?? ''} options={providers.map(p => p.id)}
            onChange={(v) => { const p = providers.find(p => p.id === v); selectAPIModel(p, p?.models[0] ?? '') }} />
        </Field>
      )}
      <Field label="模型">
        {source === 'galaxy_api'
          ? <Select value={cfg.model} options={withValue(provider?.models ?? [], cfg.model)} onChange={(v) => selectAPIModel(provider, v)} />
          : <Select value={cfg.model} options={withValue(models[cfg.backend] ?? [], cfg.model)} onChange={(v) => onChange({ ...cfg, model: v, ...(v === 'gpt-6-astra' && !cfg.effort ? { effort: 'medium' } : {}) })} />}
      </Field>
      <Field label="effort">
        <Select value={cfg.effort ?? ''} options={EFFORTS} onChange={(v) => onChange({ ...cfg, effort: v || undefined })} />
      </Field>
      {cfg.backend !== 'claude' && models.galaxy_api && (
        <Field label="请求档位（fast 不保证实际兑现）">
          <Select value={cfg.service_tier ?? 'default'} options={['default', 'fast']}
            onChange={(v) => onChange({ ...cfg, service_tier: v })} />
        </Field>
      )}
    </div>
  )
}

function Group({ title, children, className = '' }: { title: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-white/[0.05] bg-black/20 p-4 ${className}`}>
      <div className="mb-3 text-xs font-medium text-slate-400">{title}</div>
      {children}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block"><span className="mb-1 block text-[11px] text-slate-500">{label}</span>{children}</label>
}

const INPUT = 'w-full rounded-lg border border-white/10 bg-black/30 px-3 py-1.5 text-sm text-white outline-none focus:border-violet-400/60'

// Full option list plus the saved value, so an existing (possibly unlisted) choice is never silently changed.
const withValue = (options: string[], value?: string) => Array.from(new Set([...options, ...(value ? [value] : [])]))

function Select({ value, options, onChange }: { value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={INPUT}>
      {options.map((o) => <option key={o} value={o} className="bg-abyss-900">{o || '默认'}</option>)}
    </select>
  )
}
