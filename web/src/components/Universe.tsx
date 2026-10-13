import { Fragment, useEffect, useState } from 'react'
import { Activity, Brain, Check, Cpu, Globe2, Loader2, Pencil, Radio, ShieldCheck, Trash2, X } from 'lucide-react'
import { Agent, AdmissionContact, ago, api, Directory, Memory, Message, Patrol, ROLE_META, timeLabel, tokens } from '../api'
import { Avatar, stateOf } from './Avatar'
import { RuntimeBadge } from './RuntimeBadge'
import { MessageRow } from './Messages'
import { Deploy } from './Deploy'

const ROLE_TITLE: Record<string, string> = { azathoth: '意志的化身', shub: '万物之母 · 组织落地', yog: '门与钥匙 · 监督', ghroth: '维护者 · sudo' }

export function Universe({ dir, patrolTick, deployTick }: { dir: Directory; patrolTick: number; deployTick: number }) {
  const [selected, setSelected] = useState<Agent | null>(null)
  const [patrols, setPatrols] = useState<Patrol[]>([])
  const gods = dir.agents.filter((a) => a.role !== 'old_one')
  const olds = dir.agents.filter((a) => a.role === 'old_one' && a.desired === 'running')
  const retired = dir.agents.filter((a) => a.role === 'old_one' && a.desired === 'stopped')
  const working = dir.agents.filter((a) => a.online && a.state === 'working').length
  const online = dir.agents.filter((a) => a.online).length

  useEffect(() => { api<Patrol[]>('patrols?limit=5').then(setPatrols).catch(() => {}) }, [patrolTick])
  useEffect(() => { if (selected) setSelected(dir.agents.find((a) => a.id === selected.id) ?? null) }, [dir]) // eslint-disable-line

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl space-y-8 px-4 py-6 sm:px-8 sm:py-8">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat icon={<Radio className="h-4 w-4" />} label="在线 agent" value={`${online}/${dir.agents.filter((a) => a.desired === 'running').length}`} tone="from-emerald-500/20 to-teal-500/5 text-emerald-300" />
          <Stat icon={<Cpu className="h-4 w-4" />} label="工作中" value={working} tone="from-amber-500/20 to-orange-500/5 text-amber-300" />
          <Stat icon={<Activity className="h-4 w-4" />} label="旧日支配者" value={olds.length} tone="from-cyan-500/20 to-sky-500/5 text-cyan-300" />
          <Stat icon={<Globe2 className="h-4 w-4" />} label="星系" value={`${dir.galaxies.filter((g) => g.online).length}/${dir.galaxies.length}`} tone="from-violet-500/20 to-fuchsia-500/5 text-violet-300" />
        </div>

        <Section title="诸神">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {gods.map((a) => <AgentCard key={a.id} agent={a} galaxies={dir} onClick={() => setSelected(a)} />)}
          </div>
        </Section>

        <Section title="旧日支配者" hint={olds.length ? undefined : '尚未创建。黑山羊会根据阿撒托斯的意志创建它们。'}>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {olds.map((a) => <AgentCard key={a.id} agent={a} galaxies={dir} onClick={() => setSelected(a)} wide />)}
          </div>
          {retired.length > 0 && (
            <details className="mt-3 text-sm text-slate-500">
              <summary className="cursor-pointer hover:text-slate-300">已终止 {retired.length} 个</summary>
              <div className="mt-3 grid gap-3 opacity-60 sm:grid-cols-2 lg:grid-cols-3">
                {retired.map((a) => <AgentCard key={a.id} agent={a} galaxies={dir} onClick={() => setSelected(a)} wide />)}
              </div>
            </details>
          )}
        </Section>

        <Deploy dir={dir} tick={deployTick} />

        <Section title="尤格的巡检">
          {patrols.length === 0 ? (
            <p className="text-sm text-slate-500">还没有巡检记录。尤格每 5 分钟检查一次，宇宙无变化时保持安静。</p>
          ) : (
            <ol className="relative space-y-3 border-l border-amber-400/20 pl-5">
              {patrols.slice().reverse().map((p) => (
                <li key={p.id} className="relative">
                  <span className="absolute -left-[25px] top-1.5 h-2.5 w-2.5 rounded-full bg-amber-400 ring-4 ring-amber-400/10" />
                  <p className="text-xs text-amber-300/70">{timeLabel(p.created_at)}</p>
                  <p className="mt-0.5 whitespace-pre-wrap text-sm text-slate-300">{p.summary}</p>
                </li>
              ))}
            </ol>
          )}
        </Section>
      </div>
      {selected && <AgentDrawer agent={selected} dir={dir} onClose={() => setSelected(null)} />}
    </div>
  )
}

function Stat({ icon, label, value, tone }: { icon: React.ReactNode; label: string; value: React.ReactNode; tone: string }) {
  return (
    <div className={`rounded-2xl border border-white/[0.06] bg-gradient-to-br ${tone} p-4`}>
      <div className="flex items-center gap-2 text-xs opacity-90">{icon}{label}</div>
      <div className="mt-2 text-2xl font-semibold text-white">{value}</div>
    </div>
  )
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">{title}</h3>
      {hint ? <p className="text-sm text-slate-500">{hint}</p> : children}
    </section>
  )
}

function AgentCard({ agent, galaxies, onClick, wide }: { agent: Agent; galaxies: Directory; onClick: () => void; wide?: boolean }) {
  const m = ROLE_META[agent.role]
  const st = stateOf(agent)
  const g = galaxies.galaxies.find((x) => x.id === agent.galaxy_id)
  return (
    <button onClick={onClick}
      className="group relative overflow-hidden rounded-2xl border border-white/[0.07] bg-white/[0.03] p-4 text-left transition hover:-translate-y-0.5 hover:border-white/15 hover:bg-white/[0.05] hover:shadow-xl hover:shadow-black/30">
      <div className={`absolute inset-x-0 top-0 h-px bg-gradient-to-r ${m.grad} opacity-60`} />
      <div className="flex items-start gap-3">
        <Avatar role={agent.role} live={agent} />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium text-white">{agent.name}</p>
          <p className={`text-xs ${m.text}`}>{agent.role === 'old_one' ? m.label : ROLE_TITLE[agent.role]}</p>
        </div>
        <span className={`chip ${st.chip}`}>{st.label}</span>
      </div>
      {wide && agent.task && <p className="mt-3 line-clamp-2 text-xs text-slate-400">{agent.task}</p>}
      <div className="mt-3 min-h-[1.25rem] text-xs text-slate-500">
        {agent.state === 'working' && agent.current ? <p className="line-clamp-2 text-amber-200/80">⟳ {agent.current}</p> : <span>{g?.name ?? agent.galaxy_id}{agent.queued > 0 && ` · 队列 ${agent.queued}`}</span>}
      </div>
      {agent.runtime && <div className="mt-2"><RuntimeBadge runtime={agent.runtime} compact /></div>}
      {agent.role !== 'old_one' && agent.memory && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-slate-500">
          <Brain className={`h-3.5 w-3.5 ${m.text}`} />
          永久上下文 {agent.memory.entries} 条 · ≈{tokens(agent.memory.tokens)} tokens
        </p>
      )}
      {agent.context_fill != null && agent.context_fill > 0 && (
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/5" title={`上下文占用 ${(agent.context_fill * 100).toFixed(0)}%`}>
          <div className={`h-full bg-gradient-to-r ${m.grad}`} style={{ width: `${Math.min(agent.context_fill * 100, 100)}%` }} />
        </div>
      )}
    </button>
  )
}

function AgentDrawer({ agent, dir, onClose }: { agent: Agent; dir: Directory; onClose: () => void }) {
  const [tab, setTab] = useState<'latest' | 'memory' | 'admission' | 'messages'>('latest')
  const [latest, setLatest] = useState<Record<string, unknown> | null>(null)
  const [memory, setMemory] = useState<Memory[] | null>(null)
  const [msgs, setMsgs] = useState<Message[] | null>(null)
  const m = ROLE_META[agent.role]
  const hasMemory = ['azathoth', 'shub', 'yog', 'ghroth', 'nyarlathotep'].includes(agent.role)
  const hasAdmission = agent.role === 'nyarlathotep'

  useEffect(() => {
    setLatest(null); setMemory(null); setMsgs(null)
    api<Record<string, unknown>>(`latest?agent_id=${agent.id}`).then(setLatest).catch((e) => setLatest({ error: e.message }))
    api<Message[]>(`messages?agent_id=${agent.id}&limit=60`).then(setMsgs)
  }, [agent.id])

  // Refetch whenever the directory reports a memory change for this agent.
  useEffect(() => {
    if (hasMemory) api<Memory[]>(`memory?agent_id=${agent.id}`).then(setMemory).catch(() => setMemory([]))
  }, [agent.id, hasMemory, agent.memory?.entries, agent.memory?.updated_at])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <aside className="relative flex h-full w-full max-w-xl flex-col border-l border-white/10 bg-abyss-900/95 shadow-2xl backdrop-blur-xl animate-rise">
        <div className={`h-1 bg-gradient-to-r ${m.grad}`} />
        <header className="flex shrink-0 items-start gap-3 p-5">
          <Avatar role={agent.role} size="lg" live={agent} />
          <div className="min-w-0 flex-1">
            <h3 className="text-lg font-semibold text-white">{agent.name}</h3>
            <p className="text-xs text-slate-500">{m.label} · <span className="font-mono">{agent.id}</span></p>
            <p className="text-xs text-slate-500">创建于 {ago(agent.created_at)}</p>
            {agent.runtime && <div className="mt-1.5"><RuntimeBadge runtime={agent.runtime} /></div>}
          </div>
          <button className="btn-icon" onClick={onClose}><X className="h-5 w-5" /></button>
        </header>
        {agent.task && <details className="mx-5 mb-4 shrink-0 rounded-xl border border-white/[0.06] bg-white/[0.03] text-sm text-slate-300">
          <summary className="cursor-pointer p-3">任务与职责</summary>
          <div className="max-h-[25vh] overflow-y-auto whitespace-pre-wrap break-words px-3 pb-3">{agent.task}</div>
        </details>}
        <nav className="flex shrink-0 gap-1 border-b border-white/[0.06] px-5">
          {(['latest', ...(hasMemory ? ['memory'] : []), ...(hasAdmission ? ['admission'] : []), 'messages'] as const).map((t) => (
            <button key={t} onClick={() => setTab(t as typeof tab)}
              className={`relative px-3 py-2.5 text-sm transition ${tab === t ? 'text-white' : 'text-slate-500 hover:text-slate-300'}`}>
              {{ latest: '最新状态', memory: '永久上下文', admission: '准入配置', messages: '消息' }[t]}
              {tab === t && <span className={`absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-gradient-to-r ${m.grad}`} />}
            </button>
          ))}
        </nav>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {tab === 'latest' && (latest ? <Latest data={latest} /> : <Spinner />)}
          {tab === 'memory' && (memory ? (
            memory.length ? (
              <>
              <p className="mb-3 text-xs text-slate-500">{memory.length} 条 · 约 {tokens(agent.memory?.tokens ?? 0)} tokens（上限规划 100k）</p>
              <ul className="space-y-2">
                {memory.map((x) => <MemoryRow key={x.id} agentId={agent.id} item={x} tone={m.text} onChange={setMemory} />)}
              </ul>
              </>
            ) : <p className="text-sm text-slate-500">永久上下文为空。</p>
          ) : <Spinner />)}
          {tab === 'admission' && <Admission data={agent.admission} online={agent.online} />}
          {tab === 'messages' && (msgs ? (
            msgs.length ? <div className="space-y-2">{msgs.slice().reverse().map((x) => <MessageRow key={x.id} m={x} dir={dir} compact />)}</div>
              : <p className="text-sm text-slate-500">没有消息。</p>
          ) : <Spinner />)}
        </div>
      </aside>
    </div>
  )
}

const STATE_LABEL: Record<string, string> = {
  idle: '空闲', working: '工作中', starting: '启动中', completed: '完成', interrupted: '已中断', failed: '失败',
}

function MemoryRow({ agentId, item, tone, onChange }: { agentId: string; item: Memory; tone: string; onChange: (m: Memory[]) => void }) {
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const updated = item.updated_at ?? item.created_at
  const save = async (body: Record<string, unknown>) => {
    setBusy(true)
    try { onChange(await api<Memory[]>('memory', { agent_id: agentId, id: item.id, ...body })); setDraft(null) } finally { setBusy(false) }
  }
  return (
    <li className="group flex gap-3 rounded-xl bg-white/[0.03] p-3 text-sm text-slate-300">
      <Brain className={`mt-0.5 h-4 w-4 shrink-0 ${tone}`} />
      <div className="min-w-0 flex-1">
        {draft === null ? <p className="whitespace-pre-wrap break-words">{item.text}</p> : (
          <textarea autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} rows={3}
            className="w-full resize-y rounded-lg border border-white/10 bg-black/30 p-2 text-sm text-slate-200 outline-none focus:border-violet-400/50" />
        )}
        <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-500">
          <span className="font-mono">#{item.id}</span>
          <span>更新于 {timeLabel(updated)}</span>
          {updated - item.created_at > 1 && <span>· 创建于 {timeLabel(item.created_at)}</span>}
          {item.source === 'owner' && <span className="chip bg-violet-500/15 text-violet-300">主人修改</span>}
          <span className="ml-auto flex gap-1 opacity-0 transition group-hover:opacity-100 max-sm:opacity-100">
            {draft === null ? (
              <>
                <button className="btn-icon h-6 w-6" title="编辑" onClick={() => setDraft(item.text)}><Pencil className="h-3.5 w-3.5" /></button>
                <button className="btn-icon h-6 w-6 hover:text-rose-300" title="删除" disabled={busy}
                  onClick={() => confirm(`删除 #${item.id}？`) && save({ delete: true })}><Trash2 className="h-3.5 w-3.5" /></button>
              </>
            ) : (
              <>
                <button className="btn-icon h-6 w-6 hover:text-emerald-300" title="保存" disabled={busy || !draft.trim()} onClick={() => save({ text: draft })}><Check className="h-3.5 w-3.5" /></button>
                <button className="btn-icon h-6 w-6" title="取消" onClick={() => setDraft(null)}><X className="h-3.5 w-3.5" /></button>
              </>
            )}
          </span>
        </div>
      </div>
    </li>
  )
}

function Admission({ data, online }: { data?: Agent['admission']; online: boolean }) {
  if (!data) return <p className="text-sm text-slate-500">{online ? '尚未收到准入配置。' : '奈亚离线，无法读取准入配置。'}</p>
  const contacts = Object.entries(data.config?.contacts ?? {})
  return (
    <div className="space-y-3 text-sm text-slate-300">
      <p className="text-xs text-slate-500">
        由奈亚自行维护，每次状态更新时从 <span className="break-all font-mono">{data.path}</span> 读取
        {data.config && <> · 默认 {data.config.default === 'deny' ? '拒绝' : data.config.default ?? '未设置'} · 版本 {data.config.version ?? '?'}</>}
      </p>
      {data.error ? <p className="text-rose-300">{data.error}</p>
        : contacts.length ? contacts.map(([name, c]) => <AdmissionEntry key={name} name={name} c={c} />)
        : <p className="text-slate-500">没有登记任何联系人（全部拒绝）。</p>}
    </div>
  )
}

function AdmissionEntry({ name, c }: { name: string; c: AdmissionContact }) {
  const { identity, allow, deny, scope, ...rest } = c
  const resources = scope?.resources ?? []
  const list = (items: string[] | undefined, tone: string) => items?.length
    ? <ul className="list-disc space-y-1 pl-5">{items.map((x, i) => <li key={i} className={`whitespace-pre-wrap break-words ${tone}`}>{x}</li>)}</ul>
    : <p className="text-slate-500">（无）</p>
  return (
    <details open className="rounded-xl border border-white/[0.06] bg-white/[0.03]">
      <summary className="flex cursor-pointer items-center gap-2 p-3 font-medium text-white">
        <ShieldCheck className="h-4 w-4 text-emerald-300" />{name}
      </summary>
      <div className="space-y-3 px-3 pb-3">
        <div className="space-y-0.5 text-xs text-slate-400">
          {Object.entries(identity ?? {}).map(([k, v]) => <p key={k}><span className="text-slate-500">{k}</span> <span className="break-all font-mono">{v}</span></p>)}
        </div>
        <div><p className="mb-1 text-xs text-slate-500">允许</p>{list(allow, 'text-emerald-200/90')}</div>
        <div><p className="mb-1 text-xs text-slate-500">禁止</p>{list(deny, 'text-rose-200/90')}</div>
        {scope && (
          <details className="rounded-lg bg-black/20">
            <summary className="cursor-pointer p-2 text-xs text-slate-400">范围：{scope.name ?? '未命名'} · {resources.length} 项资源</summary>
            <ul className="max-h-[40vh] space-y-2 overflow-y-auto px-2 pb-2 text-xs">
              {resources.map((r, i) => (
                <li key={i} className="rounded bg-white/[0.03] p-2">
                  <p>{[r.date, r.role, r.type].filter(Boolean).join(' · ')}</p>
                  {r.url ? <a className="break-all font-mono text-sky-300 hover:underline" href={r.url} target="_blank" rel="noreferrer">{r.url}</a>
                    : <p className="break-all font-mono">{r.token}</p>}
                  {r.evidence && <p className="mt-0.5 text-slate-500">{r.evidence}</p>}
                </li>
              ))}
            </ul>
            {scope.maintenance && <p className="whitespace-pre-wrap px-2 pb-2 text-xs text-slate-400">维护规则：{scope.maintenance}</p>}
          </details>
        )}
        {Object.keys(rest).length > 0 && (
          <details className="rounded-lg bg-black/20">
            <summary className="cursor-pointer p-2 text-xs text-slate-400">其他字段</summary>
            <pre className="max-h-[30vh] overflow-auto whitespace-pre-wrap break-words px-2 pb-2 text-[11px] text-slate-400">{JSON.stringify(rest, null, 2)}</pre>
          </details>
        )}
      </div>
    </details>
  )
}

function Latest({ data }: { data: Record<string, unknown> }) {
  if (data.online === false) return <p className="text-sm text-slate-500">该 agent 当前离线。</p>
  const rows: [string, unknown][] = [
    ['状态', STATE_LABEL[String(data.state)] ?? data.state], ['当前', data.current], ['队列', data.queued],
    ['上次结果', STATE_LABEL[String(data.last_status)] ?? data.last_status], ['完成于', typeof data.finished_at === 'number' ? timeLabel(data.finished_at) : null],
  ]
  const reply = (data.last_reply ?? data.last_patrol) as string | undefined
  const live = (data.live ?? data.activity) as string[] | undefined
  return (
    <div className="space-y-5 text-sm">
      <dl className="grid grid-cols-[5rem_1fr] gap-x-3 gap-y-2">
        {rows.filter(([, v]) => v != null && v !== '').map(([k, v]) => (
          <Fragment key={k}><dt className="text-slate-500">{k}</dt><dd className="text-slate-200">{String(v)}</dd></Fragment>
        ))}
      </dl>
      {typeof data.error === 'string' && <p className="rounded-xl bg-rose-500/10 p-3 text-rose-300">{data.error}</p>}
      {live && live.length > 0 && (
        <div>
          <p className="mb-2 text-xs text-slate-500">最近动作</p>
          <ul className="space-y-1 rounded-xl bg-black/30 p-3 font-mono text-xs text-slate-400">{live.map((a, i) => <li key={i} className="break-all">{a}</li>)}</ul>
        </div>
      )}
      {reply && (
        <div>
          <p className="mb-2 text-xs text-slate-500">最新产出</p>
          <p className="whitespace-pre-wrap rounded-xl bg-white/[0.03] p-3 text-slate-300">{reply}</p>
        </div>
      )}
    </div>
  )
}

function Spinner() {
  return <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-slate-600" /></div>
}
