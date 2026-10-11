import { useEffect, useState } from 'react'
import { ArrowRight, Loader2 } from 'lucide-react'
import { api, Directory, Message, Role, ROLE_META, timeLabel } from '../api'
import { Avatar } from './Avatar'

export function Messages({ dir, live }: { dir: Directory; live: Message[] }) {
  const [msgs, setMsgs] = useState<Message[] | null>(null)
  const [filter, setFilter] = useState('')
  const [more, setMore] = useState(true)

  useEffect(() => {
    setMsgs(null)
    api<Message[]>(`messages?limit=100${filter ? `&agent_id=${filter}` : ''}`).then((m) => { setMsgs(m); setMore(m.length === 100) })
  }, [filter])

  useEffect(() => {
    if (!msgs || !live.length) return
    const fresh = live.filter((m) => !msgs.some((x) => x.id === m.id) && (!filter || m.sender === filter || m.receiver === filter))
    if (fresh.length) setMsgs([...msgs, ...fresh])
  }, [live]) // eslint-disable-line

  const older = async () => {
    if (!msgs?.length) return
    const m = await api<Message[]>(`messages?limit=100&before=${msgs[0].created_at}${filter ? `&agent_id=${filter}` : ''}`)
    setMsgs([...m, ...msgs]); setMore(m.length === 100)
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-6 sm:px-8">
        <div className="mb-5 flex flex-wrap gap-2">
          <FilterChip active={!filter} onClick={() => setFilter('')}>全部</FilterChip>
          {dir.agents.filter((a) => a.desired === 'running').map((a) => (
            <FilterChip key={a.id} active={filter === a.id} onClick={() => setFilter(a.id)} role={a.role}>{a.name}</FilterChip>
          ))}
        </div>
        {!msgs ? <div className="flex justify-center py-16"><Loader2 className="h-5 w-5 animate-spin text-slate-600" /></div> : (
          <div className="space-y-2">
            {more && <button onClick={older} className="w-full rounded-xl py-2 text-sm text-slate-500 hover:bg-white/5 hover:text-slate-300">加载更早的消息</button>}
            {msgs.length === 0 && <p className="py-16 text-center text-sm text-slate-500">还没有消息往来。</p>}
            {msgs.slice().reverse().map((m) => <MessageRow key={m.id} m={m} dir={dir} />)}
          </div>
        )}
      </div>
    </div>
  )
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
        {!compact && m.state !== 'handled' && <span className="chip bg-sky-400/15 text-sky-300">{m.state === 'queued' ? '待投递' : '处理中'}</span>}
      </div>
      <p onClick={() => long && setOpen(!open)}
        className={`whitespace-pre-wrap break-words pl-9 text-sm text-slate-300 ${long ? 'cursor-pointer' : ''} ${long && !open ? 'line-clamp-4' : ''}`}>
        {m.text}
      </p>
    </div>
  )
}
