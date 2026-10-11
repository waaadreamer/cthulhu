import { useEffect, useState } from 'react'
import { Check, Loader2, RotateCcw } from 'lucide-react'
import { Agent, api, Directory, Role, ROLE_META, RuntimeCfg } from '../api'
import { Avatar } from './Avatar'
import { RuntimeBadge } from './RuntimeBadge'

type Models = Record<'claude' | 'codex', string[]>
const EFFORTS = ['', 'low', 'medium', 'high', 'xhigh', 'max']

export function Settings({ dir }: { dir: Directory }) {
  const [overrides, setOverrides] = useState<Record<string, RuntimeCfg> | null>(null)
  const [models, setModels] = useState<Models>({ claude: [], codex: [] })
  const load = () => api<{ overrides: Record<string, RuntimeCfg>; models: Models }>('runtimes')
    .then((r) => { setOverrides(r.overrides); setModels(r.models) })
  useEffect(() => { load() }, [])

  const roles: Role[] = ['azathoth', 'shub', 'yog', 'ghroth']
  const gods = roles.map((r) => dir.agents.find((a) => a.role === r)).filter(Boolean) as Agent[]
  const olds = dir.agents.filter((a) => a.role === 'old_one' && a.desired === 'running')

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-6 px-4 py-6 sm:px-8 sm:py-8">
        <div>
          <h2 className="text-lg font-semibold text-white">模型与后端</h2>
          <p className="mt-1 text-sm text-slate-400">
            修改后在该 agent 当前回合结束时生效。切换 Codex / Claude 会开启新会话，程序会把永久上下文和最近的交流记录带过去。
            主后端出错（如被限流）时自动改用后备 30 分钟。
          </p>
        </div>
        {!overrides ? <div className="flex justify-center py-16"><Loader2 className="h-5 w-5 animate-spin text-slate-600" /></div> : (
          <>
            {gods.map((a) => (
              <RuntimeEditor key={a.id} agent={a} keyName={a.role} override={overrides[a.role]} models={models} onSaved={load} />
            ))}
            {olds.length > 0 && <h3 className="pt-4 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">旧日支配者（逐个设置；未设置则用星系默认）</h3>}
            {olds.map((a) => (
              <RuntimeEditor key={a.id} agent={a} keyName={a.id} override={overrides[a.id]} models={models} onSaved={load} />
            ))}
          </>
        )}
      </div>
    </div>
  )
}

function RuntimeEditor({ agent, keyName, override, models, onSaved }: {
  agent: Agent; keyName: string; override?: RuntimeCfg; models: Models; onSaved: () => void
}) {
  const base = override ?? agent.runtime?.default ?? { backend: 'codex', model: '' }
  const [cfg, setCfg] = useState<RuntimeCfg>(base)
  const [fb, setFb] = useState<boolean>(!!base.fallback)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { const b = override ?? agent.runtime?.default ?? { backend: 'codex', model: '' }; setCfg(b); setFb(!!b.fallback) }, [override, agent.runtime?.default]) // eslint-disable-line
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
          <p className="text-xs text-slate-500">{override ? '已自定义' : '星系默认'}</p>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">当前 <RuntimeBadge runtime={agent.runtime} /></div>
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        <Group title="主后端">
          <BackendFields cfg={cfg} models={models} onChange={setCfg} />
        </Group>
        <Group title={<label className="flex cursor-pointer items-center gap-2"><input type="checkbox" checked={fb} onChange={(e) => setFb(e.target.checked)} className="accent-violet-500" />后备（主后端出错时）</label>}>
          {fb ? (
            <BackendFields cfg={cfg.fallback ?? { backend: cfg.backend === 'claude' ? 'codex' : 'claude', model: '' }} models={models}
              onChange={(f) => setCfg({ ...cfg, fallback: f })} />
          ) : <p className="text-sm text-slate-500">未启用。主后端出错时该回合直接失败。</p>}
        </Group>
      </div>

      {['azathoth', 'shub', 'yog', 'ghroth'].includes(agent.role) && cfg.backend === 'claude' && (
        <Group title="记忆提炼（在主会话分叉上运行，共享缓存）" className="mt-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="模型">
              <ModelInput value={cfg.memory_model ?? ''} options={models.claude} placeholder="同主模型" onChange={(v) => setCfg({ ...cfg, memory_model: v || undefined })} />
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

function BackendFields({ cfg, models, onChange }: { cfg: RuntimeCfg; models: Models; onChange: (c: RuntimeCfg) => void }) {
  return (
    <div className="space-y-3">
      <div className="flex gap-1 rounded-xl bg-black/30 p-1">
        {(['claude', 'codex'] as const).map((b) => (
          <button key={b} onClick={() => b !== cfg.backend && onChange({ ...cfg, backend: b, model: models[b][0] ?? '' })}
            className={`flex-1 rounded-lg py-1.5 text-sm transition ${cfg.backend === b
              ? b === 'claude' ? 'bg-orange-400/20 text-orange-100' : 'bg-sky-400/20 text-sky-100'
              : 'text-slate-500 hover:text-slate-300'}`}>
            {b === 'claude' ? 'Claude Code' : 'Codex'}
          </button>
        ))}
      </div>
      <Field label="模型">
        <ModelInput value={cfg.model} options={models[cfg.backend]} onChange={(v) => onChange({ ...cfg, model: v })} />
      </Field>
      <Field label="effort">
        <Select value={cfg.effort ?? ''} options={EFFORTS} onChange={(v) => onChange({ ...cfg, effort: v || undefined })} />
      </Field>
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

function ModelInput({ value, options, onChange, placeholder }: { value: string; options: string[]; onChange: (v: string) => void; placeholder?: string }) {
  const id = `models-${options.join('-').length}-${options[0] ?? ''}`
  return (
    <>
      <input list={id} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value.trim())} className={INPUT + ' font-mono'} />
      <datalist id={id}>{options.map((o) => <option key={o} value={o} />)}</datalist>
    </>
  )
}

function Select({ value, options, onChange }: { value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={INPUT}>
      {options.map((o) => <option key={o} value={o} className="bg-abyss-900">{o || '默认'}</option>)}
    </select>
  )
}
