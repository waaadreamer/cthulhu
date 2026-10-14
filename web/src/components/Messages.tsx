import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Loader2 } from 'lucide-react'
import { api, Directory, Message, Task, Role, ROLE_META, TASK_STATE, timeLabel } from '../api'
import { Avatar } from './Avatar'
import { TaskPanel } from './TaskPanel'

export function Messages({ dir, live, taskTick }: { dir: Directory; live: Message[]; taskTick: number }) {
  const [filter, setFilter] = useState('')
  const [task, setTask] = useState<Task | null>(null)
  return (
    <div className="flex h-full min-h-0 flex-col lg:flex-row">
      <TaskList selected={task?.id} onSelect={setTask} taskTick={taskTick} />
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-4 py-6 sm:px-8">
          <h2 className="mb-2 break-words font-semibold text-white">{task ? task.description.slice(0, 120) : '全部消息'}</h2>
          {task && <p className="mb-3 break-all text-xs text-slate-500">{task.id}</p>}
          {task && <TaskPanel key={task.id} taskId={task.id} dir={dir} tick={taskTick} />}
          <div className="mb-5 flex flex-wrap gap-2">
            <FilterChip active={!filter} onClick={() => setFilter('')}>全部角色</FilterChip>
            {dir.agents.map((a) => (
              <FilterChip key={a.id} active={filter === a.id} onClick={() => setFilter(a.id)} role={a.role}>{a.name}</FilterChip>
            ))}
          </div>
          <MessageStream key={`${filter}:${task?.id ?? ''}`} dir={dir} live={live} agentId={filter} taskId={task?.id} />
        </div>
      </div>
    </div>
  )
}

function TaskList({ selected, onSelect, taskTick }: { selected?: string; onSelect: (t: Task | null) => void; taskTick: number }) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [state, setState] = useState('')
  const [search, setSearch] = useState('')
  const [refresh, setRefresh] = useState(0)
  const generation = useRef(0)
  const [more, setMore] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const query = `tasks?limit=50&state=${state}&search=${encodeURIComponent(search)}`
  useEffect(() => {
    let cancelled = false
    generation.current += 1
    setTasks([]); setMore(false); setBusy(true); setError('')
    const timer = setTimeout(() => {
      api<Task[]>(query).then((rows) => {
        if (!cancelled) { setTasks(rows); setMore(rows.length === 50) }
      }).catch((e) => !cancelled && setError(e.message)).finally(() => !cancelled && setBusy(false))
    }, 150)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [query, refresh])
  // Live state changes (checkpoints, verdicts) refresh the list in place without resetting it.
  useEffect(() => {
    if (taskTick) api<Task[]>(query).then((rows) => setTasks((prev) => prev.map((t) => rows.find((r) => r.id === t.id) ?? t))).catch(() => {})
  }, [taskTick])
  const older = async () => {
    const current = generation.current
    setBusy(true); setError('')
    try {
      const rows = await api<Task[]>(`${query}&before_id=${encodeURIComponent(tasks[tasks.length - 1].id)}`)
      if (current !== generation.current) return
      setTasks((prev) => [...prev, ...rows.filter((t) => !prev.some((x) => x.id === t.id))]); setMore(rows.length === 50)
    } catch (e) { if (current === generation.current) setError((e as Error).message) } finally { if (current === generation.current) setBusy(false) }
  }
  return <aside className="max-h-[40vh] shrink-0 overflow-y-auto border-b border-white/10 p-4 lg:max-h-full lg:w-72 lg:border-b-0 lg:border-r">
    <div className="mb-3 flex items-center justify-between"><h2 className="font-semibold text-white">历史任务</h2><button onClick={() => setRefresh((n) => n + 1)} className="text-xs text-violet-300">刷新</button></div>
    <input aria-label="搜索历史任务" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索任务或消息"
      className="mb-2 w-full rounded-lg border border-white/10 bg-black/20 p-2 text-sm" />
    <select aria-label="任务状态" value={state} onChange={(e) => setState(e.target.value)} className="mb-3 w-full rounded-lg bg-abyss-800 p-2 text-sm">
      <option value="">全部状态</option>
      {Object.entries(TASK_STATE).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
    </select>
    <button onClick={() => onSelect(null)} className={`mb-2 w-full rounded-lg p-2 text-left text-sm ${!selected ? 'bg-white/10 text-white' : 'text-slate-400'}`}>全部消息</button>
    {tasks.map((t) => {
      const st = TASK_STATE[t.state] ?? TASK_STATE.running
      return <button key={t.id} onClick={() => onSelect(t)} data-history-task={t.id}
        className={`mb-1 w-full rounded-lg p-2 text-left ${selected === t.id ? 'bg-white/10 text-white' : 'text-slate-400 hover:bg-white/5'}`}>
        <span className="block break-words text-sm line-clamp-2">{t.description}</span>
        <span className="text-xs text-slate-500"><span className={`chip ${st.chip}`}>{st.label}</span> · {t.executor_name ?? '—'} · {timeLabel(t.created_at)}</span>
        {t.line && <span className="block truncate text-[11px] text-slate-600">{t.line}</span>}
      </button>
    })}
    {error && <p role="alert" className="text-sm text-rose-300">{error}</p>}
    {busy && <p className="py-2 text-xs text-slate-500">加载中…</p>}
    {!busy && !tasks.length && !error && <p className="py-2 text-sm text-slate-500">没有匹配的任务。</p>}
    {more && <button disabled={busy} onClick={older} className="w-full py-3 text-sm text-violet-300 disabled:opacity-40">加载更早的任务</button>}
  </aside>
}

function MessageStream({ dir, live, agentId, taskId }: { dir: Directory; live: Message[]; agentId: string; taskId?: string }) {
  const [msgs, setMsgs] = useState<Message[] | null>(null)
  const [more, setMore] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const query = `messages?limit=100&agent_id=${encodeURIComponent(agentId)}&task_id=${encodeURIComponent(taskId ?? '')}`
  useEffect(() => {
    let cancelled = false
    api<Message[]>(query).then((m) => { if (!cancelled) { setMsgs(m); setMore(m.length === 100) } })
      .catch((e) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [query])
  const older = async () => {
    if (!msgs?.length) return
    setBusy(true); setError('')
    try {
      const rows = await api<Message[]>(`${query}&before_id=${encodeURIComponent(msgs[0].id)}`)
      setMsgs((prev) => [...rows, ...(prev ?? [])]); setMore(rows.length === 100)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const merged = new Map((msgs ?? []).map((m) => [m.id, m]))
  for (const m of live) if ((!agentId || m.sender === agentId || m.receiver === agentId) && (!taskId || m.task_id === taskId)) merged.set(m.id, m)
  const shown = [...merged.values()].sort((a, b) => b.created_at - a.created_at || b.id.localeCompare(a.id))
  return <div className="space-y-2" data-message-stream>
    {error && <p role="alert" className="text-sm text-rose-300">{error}</p>}
    {!msgs && !error && <div className="flex justify-center py-16"><Loader2 className="h-5 w-5 animate-spin text-slate-600" /></div>}
    {more && <button disabled={busy} onClick={older} className="w-full rounded-xl py-2 text-sm text-slate-400 hover:bg-white/5 disabled:opacity-40">{busy ? '加载中…' : '加载更早的消息'}</button>}
    {msgs && !shown.length && <p className="py-16 text-center text-sm text-slate-500">还没有消息往来。</p>}
    {shown.map((m) => <MessageRow key={m.id} m={m} dir={dir} />)}
  </div>
}

function FilterChip({ active, onClick, role, children }: { active: boolean; onClick: () => void; role?: Role; children: React.ReactNode }) {
  const c = role ? ROLE_META[role].text : 'text-slate-300'
  return (
    <button onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs transition ${active ? `border-white/20 bg-white/10 ${c}` : 'border-white/[0.06] text-slate-500 hover:border-white/15 hover:text-slate-300'}`}>
      {children}
    </button>
  )
}

export function MessageRow({ m, dir, compact }: { m: Message; dir: Directory; compact?: boolean }) {
  const [open, setOpen] = useState(false)
  const who = (id: string) => dir.agents.find((a) => a.id === id)
  const s = who(m.sender), r = who(m.receiver)
  const long = m.text.length > 280
  return (
    <div className="rounded-2xl border border-white/[0.05] bg-white/[0.025] p-3.5 transition hover:border-white/10">
      <div className="mb-2 flex items-center gap-2 text-xs">
        {s ? <Avatar role={s.role} size="sm" /> : <div className="grid h-7 w-7 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-sm text-white">✦</div>}
        <span className={s ? ROLE_META[s.role].text : 'text-indigo-300'}>{s?.name ?? '主人'}</span>
        <ArrowRight className="h-3 w-3 text-slate-600" />
        <span className={r ? ROLE_META[r.role].text : 'text-slate-400'}>{r?.name ?? m.receiver}</span>
        {m.priority > 0 && <span className="chip bg-rose-500/15 text-rose-300">优先 {m.priority}</span>}
        <span className="ml-auto shrink-0 text-slate-600">{timeLabel(m.created_at)}</span>
        {!compact && m.state !== 'handled' && m.state !== 'cancelled' && <span className="chip bg-sky-400/15 text-sky-300">{m.state === 'queued' ? '待投递' : '处理中'}</span>}
        {m.state === 'cancelled' && <span className="chip bg-slate-500/15 text-slate-400" title="接收方被强制停止，未处理">已取消</span>}
      </div>
      <p onClick={() => long && setOpen(!open)}
        className={`whitespace-pre-wrap break-words pl-9 text-sm text-slate-300 ${long ? 'cursor-pointer' : ''} ${long && !open ? 'line-clamp-4' : ''}`}>
        {m.text}
      </p>
    </div>
  )
}
