import { Fragment, useEffect, useState } from 'react'
import { Activity, Brain, Check, Cpu, Globe2, Loader2, Pencil, Radio, ShieldCheck, Trash2, X } from 'lucide-react'
import { Agent, ago, api, Directory, Memory, Message, ROLE_META, timeLabel, tokens } from '../api'
import { Avatar, stateOf } from './Avatar'
import { RuntimeBadge } from './RuntimeBadge'
import { MessageRow } from './Messages'
import { Deploy } from './Deploy'
import { ForceStop } from './ForceStop'

const ROLE_TITLE: Record<string, string> = { azathoth: '唯一的决策者 · 维护者', nyarlathotep: '飞书入口' }

export function Universe({ dir, deployTick }: { dir: Directory; deployTick: number }) {
  const [selected, setSelected] = useState<Agent | null>(null)
  const gods = dir.agents.filter((a) => a.role !== 'old_one')
  const olds = dir.agents.filter((a) => a.role === 'old_one' && a.desired === 'running')
  const retired = dir.agents.filter((a) => a.role === 'old_one' && a.desired === 'stopped')
  const working = dir.agents.filter((a) => a.online && a.state === 'working').length
  const online = dir.agents.filter((a) => a.online).length

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

        <Section title="旧日支配者" hint={olds.length ? undefined : '尚未创建。阿撒托斯派任务时，星系枢纽会创建或复用它们。'}>
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
        <ForceStop agent={agent} compact />
      </div>
      {agent.desired === 'stopped' && agent.stopped_by && <p className="mt-2 text-xs text-rose-300/80">由{agent.stopped_by}强制停止 · 拒收消息直至恢复</p>}
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
  const hasMemory = ['azathoth', 'nyarlathotep'].includes(agent.role)
  const hasAdmission = agent.role === 'nyarlathotep'

  useEffect(() => {
    setLatest(null); setMemory(null); setMsgs(null)
    api<Record<string, unknown>>(`latest?agent_id=${agent.id}`).then(setLatest).catch((e) => setLatest({ error: e.message }))
    api<Message[]>(agent.role === 'old_one' ? `hub_messages?agent_id=${agent.id}&limit=60` : `messages?agent_id=${agent.id}&limit=60`).then(setMsgs)
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
          <ForceStop agent={agent} />
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

function admissionRecord(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function admissionText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '（无）'
}

function AdmissionValue({ value }: { value: unknown }) {
  return <pre className="max-h-[30vh] overflow-auto whitespace-pre-wrap break-words text-xs font-sans">{admissionText(value)}</pre>
}

function Admission({ data, online }: { data?: Agent['admission']; online: boolean }) {
  if (!data) return <p className="text-sm text-slate-500">{online ? '尚未收到准入配置。' : '奈亚离线，无法读取准入配置。'}</p>
  const config = admissionRecord(data.config)
  const contactMap = admissionRecord(config?.contacts)
  const contacts = Object.entries(contactMap ?? {})
  const { contacts: _contacts, default: _default, version: _version, ...extra } = config ?? {}
  return (
    <div className="space-y-3 text-sm text-slate-300">
      <p className="text-xs text-slate-500">
        由奈亚自行维护，每次状态更新时从 <span className="break-all font-mono">{admissionText(data.path)}</span> 读取
        {config && <> · 默认 {config.default === 'deny' ? '拒绝' : admissionText(config.default ?? '未设置')} · 版本 {admissionText(config.version ?? '?')}</>}
      </p>
      {data.error ? <p className="text-rose-300">{admissionText(data.error)}</p>
        : !config ? <><p className="text-slate-500">准入配置为空或格式无法识别。</p><AdmissionValue value={data.config} /></>
        : config.contacts != null && !contactMap ? <><p className="text-slate-500">联系人格式无法识别，原始内容：</p><AdmissionValue value={config.contacts} /></>
        : contacts.length ? contacts.map(([name, value]) => <AdmissionEntry key={name} name={name} value={value} />)
        : <p className="text-slate-500">没有登记任何联系人。</p>}
      {Object.keys(extra).length > 0 && <AdmissionValue value={extra} />}
    </div>
  )
}

function AdmissionEntry({ name, value }: { name: string; value: unknown }) {
  const contact = admissionRecord(value)
  const { identity, allow, deny, scope, ...rest } = contact ?? {}
  const scopeRecord = admissionRecord(scope)
  const { resources, ...scopeFields } = scopeRecord ?? {}
  const resourceItems = Array.isArray(resources) ? resources : resources == null ? [] : [resources]
  const list = (items: unknown, tone: string) => items == null || (Array.isArray(items) && !items.length)
    ? <p className="text-slate-500">（无）</p>
    : <ul className="list-disc space-y-1 pl-5">{(Array.isArray(items) ? items : [items]).map((x, i) => <li key={i} className={tone}><AdmissionValue value={x} /></li>)}</ul>
  return (
    <details open className="rounded-xl border border-white/[0.06] bg-white/[0.03]">
      <summary className="flex cursor-pointer items-center gap-2 p-3 font-medium text-white">
        <ShieldCheck className="h-4 w-4 text-emerald-300" />{name}
      </summary>
      <div className="space-y-3 px-3 pb-3">
        {!contact ? <><p className="text-xs text-slate-500">联系人内容为空或格式无法识别：</p><AdmissionValue value={value} /></> : <>
        <div className="text-xs text-slate-400"><AdmissionValue value={identity} /></div>
        <div><p className="mb-1 text-xs text-slate-500">允许</p>{list(allow, 'text-emerald-200/90')}</div>
        <div><p className="mb-1 text-xs text-slate-500">禁止</p>{list(deny, 'text-rose-200/90')}</div>
        {scope != null && (
          <details className="rounded-lg bg-black/20">
            <summary className="cursor-pointer p-2 text-xs text-slate-400">范围：{admissionText(scopeRecord?.name ?? '未命名')} · {resourceItems.length} 项资源</summary>
            <div className="px-2 pb-2 text-slate-400"><AdmissionValue value={scopeRecord ? scopeFields : scope} /></div>
            <ul className="max-h-[40vh] space-y-2 overflow-y-auto px-2 pb-2 text-xs">
              {resourceItems.map((resource, i) => {
                const url = admissionRecord(resource)?.url
                return <li key={i} className="rounded bg-white/[0.03] p-2">
                  <AdmissionValue value={resource} />
                  {typeof url === 'string' && /^https?:\/\//i.test(url) && <a className="break-all font-mono text-sky-300 hover:underline" href={url} target="_blank" rel="noreferrer">{url}</a>}
                </li>
              })}
            </ul>
          </details>
        )}
        {Object.keys(rest).length > 0 && (
          <details className="rounded-lg bg-black/20">
            <summary className="cursor-pointer p-2 text-xs text-slate-400">其他字段</summary>
            <div className="px-2 pb-2 text-slate-400"><AdmissionValue value={rest} /></div>
          </details>
        )}
        </>}
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
