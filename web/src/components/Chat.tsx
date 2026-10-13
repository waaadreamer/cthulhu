import { KeyboardEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp, Check, ChevronDown, CircleAlert, CircleCheck, Copy, Loader2, Search, Sparkles, Square, Terminal, X } from 'lucide-react'
import { Agent, api, modelLabel, ROLE_META, timeLabel, Turn } from '../api'
import { Stream } from '../useTemple'
import { Avatar, stateOf } from './Avatar'
import { RuntimeBadge } from './RuntimeBadge'
import { Markdown } from './Markdown'
import { ForceStop } from './ForceStop'

interface Props {
  agent: Agent
  turns: Turn[]
  setTurns: (f: (t: Turn[]) => Turn[]) => void
  streams: Record<string, Stream>
  hasMore: boolean
  loadMore: () => Promise<void>
}

export function Chat({ agent, turns, setTurns, streams, hasMore, loadMore }: Props) {
  const meta = ROLE_META[agent.role]
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [search, setSearch] = useState<string | null>(null)
  const [results, setResults] = useState<Turn[] | null>(null)
  const [atBottom, setAtBottom] = useState(true)
  const scroller = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  const prevHeight = useRef<number | null>(null)

  const running = turns.some((t) => t.status === 'running' || t.status === 'queued')
  const shown = results ?? turns

  const toBottom = useCallback((smooth = false) => {
    const el = scroller.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    if (prevHeight.current !== null) {
      el.scrollTop = el.scrollHeight - prevHeight.current
      prevHeight.current = null
    } else if (atBottom) toBottom()
  })

  useEffect(() => { toBottom(); input.current?.focus() }, [agent.id, toBottom])

  const onScroll = async () => {
    const el = scroller.current!
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80)
    if (el.scrollTop < 60 && hasMore && !results && prevHeight.current === null) {
      prevHeight.current = el.scrollHeight
      await loadMore()
    }
  }

  const send = async () => {
    const t = text.trim()
    if (!t || sending) return
    setSending(true)
    try {
      const m = await api<{ id: string; created_at: number }>('send', { to: agent.id, text: t })
      setTurns((ts) => (ts.some((x) => x.id === m.id) ? ts : [...ts, {
        id: m.id, agent_id: agent.id, user_text: t, reply: '', activity: [], status: 'queued',
        created_at: m.created_at, updated_at: m.created_at,
      }]))
      setText('')
      setAtBottom(true)
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setSending(false)
      input.current?.focus()
    }
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }

  useEffect(() => {
    const el = input.current
    if (el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 240) + 'px' }
  }, [text])

  useEffect(() => {
    if (search === null || !search.trim()) { setResults(null); return }
    const id = setTimeout(() => {
      api<Turn[]>(`chat?agent_id=${agent.id}&search=${encodeURIComponent(search)}&limit=100`).then(setResults)
    }, 300)
    return () => clearTimeout(id)
  }, [search, agent.id])

  const st = stateOf(agent)
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-3 border-b border-white/[0.06] px-4 py-3 sm:px-6">
        <Avatar role={agent.role} live={agent} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h2 className="truncate font-semibold text-white">{agent.name}</h2>
            <span className={`chip ${st.chip}`}>{st.label}</span>
            <RuntimeBadge runtime={agent.runtime} />
          </div>
          <p className="truncate text-xs text-slate-500">
            {agent.desired === 'stopped' ? '已停止：拒收消息，恢复后才能对话' : agent.state === 'working' && agent.current ? `正在处理：${agent.current}` : agent.role === 'azathoth' ? '你意志的化身' : '独立维护者 · 只听你和阿撒托斯的'}
          </p>
        </div>
        <ForceStop agent={agent} />
        {search === null ? (
          <button className="btn-icon" onClick={() => setSearch('')} aria-label="搜索"><Search className="h-4 w-4" /></button>
        ) : (
          <div className="flex items-center gap-1 rounded-xl border border-white/10 bg-black/30 pl-3">
            <Search className="h-4 w-4 text-slate-500" />
            <input autoFocus value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索历史"
              className="w-32 bg-transparent py-1.5 text-sm outline-none sm:w-48" />
            <button className="btn-icon h-8 w-8" onClick={() => setSearch(null)}><X className="h-4 w-4" /></button>
          </div>
        )}
      </header>

      <div ref={scroller} onScroll={onScroll} className="relative min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-6 px-4 py-6 sm:px-6">
          {results && <p className="text-center text-xs text-slate-500">找到 {results.length} 条相关对话</p>}
          {!results && hasMore && <div className="flex justify-center"><Loader2 className="h-4 w-4 animate-spin text-slate-600" /></div>}
          {!results && turns.length === 0 && <Empty agent={agent} />}
          {group(shown).map((g) => g.kind === 'task'
            ? <TaskView key={g.key} turns={g.turns} agent={agent} streams={streams} />
            : <TurnView key={g.key} turn={g.turns[0]} agent={agent} stream={streams[g.turns[0].id]} highlight={search ?? undefined} />)}
        </div>
      </div>

      <div className="relative px-3 pb-[max(env(safe-area-inset-bottom),12px)] pt-2 sm:px-6">
        {!atBottom && (
          <button onClick={() => toBottom(true)}
            className="absolute -top-11 left-1/2 grid h-9 w-9 -translate-x-1/2 place-items-center rounded-full border border-white/10 bg-abyss-800/90 text-slate-300 shadow-lg backdrop-blur hover:text-white">
            <ChevronDown className="h-4 w-4" />
          </button>
        )}
        <div className={`glass mx-auto flex max-w-3xl items-end gap-2 rounded-2xl p-2 shadow-xl shadow-black/30 transition focus-within:border-white/15 focus-within:ring-4 ${meta.ring}`}>
          <textarea
            ref={input}
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            placeholder={`对${agent.name}说…`}
            className="max-h-60 min-h-[40px] flex-1 resize-none bg-transparent px-3 py-2 text-[15px] leading-relaxed text-white placeholder:text-slate-500 outline-none"
          />
          {running && (
            <button onClick={() => api('stop', { agent_id: agent.id })} title="停止当前回合"
              className="grid h-10 w-10 place-items-center rounded-xl bg-white/10 text-slate-200 transition hover:bg-white/20">
              <Square className="h-4 w-4 fill-current" />
            </button>
          )}
          <button onClick={send} disabled={!text.trim() || sending}
            className={`grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br ${meta.grad} text-white shadow-lg transition hover:brightness-110 disabled:opacity-30 disabled:shadow-none`}>
            {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUp className="h-5 w-5" />}
          </button>
        </div>
        <p className="mt-1.5 hidden text-center text-[11px] text-slate-600 sm:block">Enter 发送 · Shift+Enter 换行</p>
      </div>
    </div>
  )
}

function Empty({ agent }: { agent: Agent }) {
  const m = ROLE_META[agent.role]
  return (
    <div className="flex flex-col items-center py-24 text-center animate-rise">
      <div className={`mb-5 grid h-20 w-20 place-items-center rounded-3xl bg-gradient-to-br ${m.grad} text-4xl text-white shadow-2xl`}>{m.glyph}</div>
      <h3 className="font-display text-xl tracking-wide text-white">{agent.name}</h3>
      <p className="mt-2 max-w-sm text-sm text-slate-400">
        {agent.role === 'azathoth' ? '你意志的化身。给它目标与洞见，它会组织整个宇宙去实现。' : '独立的 harness 维护者。它只听你和阿撒托斯的。'}
      </p>
    </div>
  )
}

function TurnView({ turn, agent, stream, highlight }: { turn: Turn; agent: Agent; stream?: Stream; highlight?: string }) {
  const live = turn.status === 'running' || turn.status === 'queued'
  const reply = live ? stream?.reply ?? '' : turn.reply
  const activity = live ? stream?.activity ?? turn.activity : turn.activity
  return (
    <div className="space-y-4 animate-rise">
      {turn.user_text && (
        <div className="flex justify-end">
          <div className="max-w-[85%] rounded-2xl rounded-br-md bg-gradient-to-br from-indigo-500/90 to-violet-600/90 px-4 py-2.5 text-[15px] leading-relaxed text-white shadow-lg shadow-indigo-950/40">
            <p className="whitespace-pre-wrap break-words">{mark(turn.user_text, highlight)}</p>
            <p className="mt-1 text-right text-[10px] text-white/50">{timeLabel(turn.created_at)}</p>
          </div>
        </div>
      )}
      <div className="flex gap-3">
        <Avatar role={agent.role} size="sm" />
        <div className="min-w-0 flex-1 pt-0.5">
          {!turn.user_text && <div className="mb-1 flex items-center gap-1.5 text-xs text-violet-300"><Sparkles className="h-3 w-3" />主动消息{turn.origin ? ` · 起因：${turn.origin}` : ''} · {timeLabel(turn.created_at)}</div>}
          {activity.length > 0 && <Activity items={activity} live={live} />}
          {reply ? (
            <Markdown text={reply} streaming={live} />
          ) : live ? (
            <Thinking queued={turn.status === 'queued'} />
          ) : null}
          {!live && <Footer turn={turn} />}
        </div>
      </div>
    </div>
  )
}

type Group = { kind: 'turn' | 'task'; key: string; turns: Turn[] }

/** One stable group per task, ordered by its latest message/update, even across owner messages. */
function group(turns: Turn[]): Group[] {
  const groups = new Map<string, Group>()
  for (const t of [...turns].sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id))) {
    const key = t.task_id ? `task-${t.task_id}` : t.id
    const g = groups.get(key)
    if (g) g.turns.push(t)
    else groups.set(key, { kind: t.task_id ? 'task' : 'turn', key, turns: [t] })
  }
  const latest = (g: Group) => Math.max(...g.turns.map((t) => Math.max(t.created_at, t.updated_at)))
  return [...groups.values()].sort((a, b) => latest(a) - latest(b) || a.key.localeCompare(b.key))
}

/** A task's reports: progress steps fold into a chain of thought. The final summary stays open, and so
 * does a blocked note while it is the latest word on the task (it summarises and asks the owner). */
function TaskView({ turns, agent, streams }: { turns: Turn[]; agent: Agent; streams: Record<string, Stream> }) {
  const latest = turns[turns.length - 1]
  const summary = [...turns].reverse().find((t) => t.kind === 'summary')
  // Done when the task is closed; a deliverable summary on a still-open task is shown open but not as "done".
  const done = !!summary && (latest.task_state ?? 'done') === 'done'
  const blocked = !done && latest.kind === 'blocked'
  const final = summary ?? (blocked ? latest : undefined)
  const finalIndex = final ? turns.indexOf(final) : turns.length
  const steps = turns.slice(0, finalIndex)
  const after = final ? turns.slice(finalIndex + 1) : []
  const [open, setOpen] = useState(!final)
  const title = turns[0].task_title || turns[0].origin || '任务'
  const last = steps[steps.length - 1]
  return (
    <div data-task-id={latest.task_id} className="flex gap-3 animate-rise">
      <Avatar role={agent.role} size="sm" />
      <div className="min-w-0 flex-1 pt-0.5">
        <div className="mb-1.5 flex items-center gap-1.5 text-xs">
          {done ? <CircleCheck className="h-3.5 w-3.5 text-emerald-400" />
            : blocked ? <CircleAlert className="h-3.5 w-3.5 text-amber-300" />
            : <Loader2 className="h-3.5 w-3.5 animate-spin text-violet-300" />}
          <span className="font-medium text-slate-200">{title}</span>
          <span className="text-slate-500">· {done ? '已完成' : blocked ? '需要你决定' : '进行中'} · {timeLabel(turns[turns.length - 1].created_at)}</span>
        </div>
        {steps.length > 0 && (
          <div className="mb-3 overflow-hidden rounded-xl border border-white/[0.06] bg-black/20">
            <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-slate-400 hover:text-slate-200">
              <Sparkles className="h-3.5 w-3.5 shrink-0 text-violet-300" />
              <span className="truncate">{open ? `过程 · ${steps.length} 步` : `过程 · ${steps.length} 步 · ${firstLine(last.reply)}`}</span>
              <ChevronDown className={`ml-auto h-3.5 w-3.5 shrink-0 transition ${open ? 'rotate-180' : ''}`} />
            </button>
            {open && (
              <ol className="space-y-2.5 border-t border-white/[0.06] px-3 py-3">
                {steps.map((t) => (
                  <li key={t.id} className="relative border-l border-white/10 pl-4 text-sm text-slate-300">
                    <span className={`absolute -left-[4.5px] top-1.5 h-2 w-2 rounded-full ${t.kind === 'blocked' ? 'bg-amber-300' : 'bg-violet-400/70'}`} />
                    <div className="mb-0.5 text-[11px] text-slate-500">{timeLabel(t.created_at)}</div>
                    {t.user_text && <p className="mb-2 whitespace-pre-wrap text-indigo-300">{t.user_text}</p>}
                    {(streams[t.id]?.activity ?? t.activity).length > 0 && <Activity items={streams[t.id]?.activity ?? t.activity} live={t.status === 'running'} />}
                    <Markdown text={streams[t.id]?.reply ?? t.reply} streaming={t.status === 'running'} />
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}
        {final && (
          <div className={blocked ? 'rounded-xl border border-amber-300/20 bg-amber-300/[0.04] px-3.5 py-2.5' : ''}>
            <Markdown text={streams[final.id]?.reply ?? final.reply} streaming={final.status === 'running'} />
            <Footer turn={final} />
          </div>
        )}
        {after.map((t) => <TurnView key={t.id} turn={t} agent={agent} stream={streams[t.id]} />)}
      </div>
    </div>
  )
}

function firstLine(text: string) {
  return text.replace(/[#*`>]/g, '').split('\n').find((l) => l.trim())?.trim() ?? ''
}

function mark(text: string, q?: string) {
  if (!q?.trim()) return text
  const i = text.toLowerCase().indexOf(q.toLowerCase())
  if (i < 0) return text
  return <>{text.slice(0, i)}<mark className="rounded bg-amber-300/30 text-white">{text.slice(i, i + q.length)}</mark>{text.slice(i + q.length)}</>
}

function Thinking({ queued }: { queued: boolean }) {
  return (
    <div className="flex items-center gap-2 py-2 text-sm text-slate-400">
      <span className="flex gap-1">
        {[0, 1, 2].map((i) => <span key={i} className="h-1.5 w-1.5 rounded-full bg-violet-400 animate-pulseDot" style={{ animationDelay: `${i * 0.2}s` }} />)}
      </span>
      {queued ? '排队中' : '思考中'}
    </div>
  )
}

function Activity({ items, live }: { items: string[]; live: boolean }) {
  const [open, setOpen] = useState(false)
  const last = items[items.length - 1]
  return (
    <div className="mb-3 overflow-hidden rounded-xl border border-white/[0.06] bg-black/20 text-xs">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-3 py-2 text-left text-slate-400 hover:text-slate-200">
        {live ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-amber-300" /> : <Terminal className="h-3.5 w-3.5 shrink-0 text-cyan-400" />}
        <span className="truncate font-mono">{open ? `${items.length} 个步骤` : last}</span>
        <ChevronDown className={`ml-auto h-3.5 w-3.5 shrink-0 transition ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <ul className="max-h-64 space-y-1 overflow-y-auto border-t border-white/[0.06] px-3 py-2 font-mono text-slate-400">
          {items.map((a, i) => <li key={i} className="break-all">{a}</li>)}
        </ul>
      )}
    </div>
  )
}

function Footer({ turn }: { turn: Turn }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="mt-1.5 flex items-center gap-2 text-[11px] text-slate-600">
      {turn.channel_receipt != null && <span className="text-slate-400" title={turn.channel_receipt.message_id}>
        飞书：{turn.channel_receipt.state === 'sent' ? '已送达' : turn.channel_receipt.state === 'ambiguous' ? '送达待核验'
          : turn.channel_receipt.state === 'failed' ? '未发送' : turn.channel_receipt.state === 'sending' ? '发送中' : '待发送'}
      </span>}
      {turn.status === 'interrupted' && <span className="chip bg-slate-500/15 text-slate-400">已停止</span>}
      {turn.status === 'failed' && <span className="chip bg-rose-500/15 text-rose-300">失败</span>}
      {turn.reply && (
        <button onClick={() => { navigator.clipboard?.writeText(turn.reply); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 hover:bg-white/5 hover:text-slate-300">
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}{copied ? '已复制' : '复制'}
        </button>
      )}
      {turn.user_text && <span>{timeLabel(turn.updated_at)}</span>}
      {turn.model && <span className="font-mono opacity-70">· {modelLabel(turn.model.split(':')[1])}</span>}
    </div>
  )
}
