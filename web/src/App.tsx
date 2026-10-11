import { useCallback, useEffect, useState } from 'react'
import { LogOut, Menu, MessagesSquare, Orbit, SlidersHorizontal, Waypoints, X } from 'lucide-react'
import { Agent, api, Message, Turn, Unauthorized } from './api'
import { useTemple } from './useTemple'
import { Backdrop } from './components/Backdrop'
import { Login } from './components/Login'
import { Chat } from './components/Chat'
import { Universe } from './components/Universe'
import { Messages } from './components/Messages'
import { Settings } from './components/Settings'
import { Avatar, StatusDot } from './components/Avatar'
import { Sigil } from './components/Sigil'
import { FilePreview } from './components/FilePreview'
import { FileRef, PreviewContext } from './preview'

type View = { kind: 'chat'; agent: string } | { kind: 'universe' } | { kind: 'messages' } | { kind: 'settings' }

export default function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined)
  useEffect(() => { api<{ user: string | null }>('session').then((r) => setUser(r.user)).catch(() => setUser(null)) }, [])
  return (
    <>
      <Backdrop />
      {user === undefined ? null : user ? <Temple onLogout={() => setUser(null)} /> : <Login onDone={setUser} />}
    </>
  )
}

const PAGE = 40

function Temple({ onLogout }: { onLogout: () => void }) {
  const [view, setView] = useState<View>({ kind: 'chat', agent: '' })
  const [menu, setMenu] = useState(false)
  const [chats, setChats] = useState<Record<string, Turn[]>>({})
  const [more, setMore] = useState<Record<string, boolean>>({})
  const [liveMsgs, setLiveMsgs] = useState<Message[]>([])
  const [patrolTick, setPatrolTick] = useState(0)
  const [preview, setPreview] = useState<FileRef | null>(null)
  const closePreview = useCallback(() => setPreview(null), [])

  const upsertTurn = useCallback((t: Turn) => {
    setChats((c) => {
      const list = c[t.agent_id]
      if (!list) return c
      const i = list.findIndex((x) => x.id === t.id)
      const next = i >= 0 ? list.map((x) => (x.id === t.id ? t : x)) : [...list, t]
      return { ...c, [t.agent_id]: next }
    })
  }, [])

  const { dir, connected, streams } = useTemple({
    onChat: upsertTurn,
    onMessage: (m) => setLiveMsgs((l) => [...l.slice(-200), m]),
    onPatrol: () => setPatrolTick((x) => x + 1),
  })

  const chatAgents = ['azathoth', 'ghroth'].map((r) => dir.agents.find((a) => a.role === r)).filter(Boolean) as Agent[]
  useEffect(() => {
    if (view.kind === 'chat' && !view.agent && chatAgents[0]) setView({ kind: 'chat', agent: chatAgents[0].id })
  }, [chatAgents, view])

  const activeId = view.kind === 'chat' ? view.agent : ''
  useEffect(() => {
    if (!activeId || chats[activeId]) return
    api<Turn[]>(`chat?agent_id=${activeId}&limit=${PAGE}`)
      .then((t) => { setChats((c) => ({ ...c, [activeId]: t })); setMore((m) => ({ ...m, [activeId]: t.length === PAGE })) })
      .catch((e) => e instanceof Unauthorized && onLogout())
  }, [activeId, chats, onLogout])

  const loadMore = async () => {
    const list = chats[activeId]
    if (!list?.length) return
    const older = await api<Turn[]>(`chat?agent_id=${activeId}&limit=${PAGE}&before=${list[0].created_at}`)
    setChats((c) => ({ ...c, [activeId]: [...older, ...c[activeId]] }))
    setMore((m) => ({ ...m, [activeId]: older.length === PAGE }))
  }

  const logout = async () => { await api('logout', {}).catch(() => {}); onLogout() }
  const go = (v: View) => { setView(v); setMenu(false) }
  const active = dir.agents.find((a) => a.id === activeId)
  const working = dir.agents.filter((a) => a.online && a.state === 'working').length

  const sidebar = (
    <nav className="flex h-full flex-col gap-1 p-3">
      <div className="mb-4 flex items-center gap-3 px-2 pt-2">
        <Sigil />
        <div>
          <p className="font-display text-base tracking-[0.2em] text-white">CTHULHU</p>
          <p className="flex items-center gap-1.5 text-[11px] text-slate-500">
            <span className={`h-1.5 w-1.5 rounded-full ${connected ? 'bg-emerald-400' : 'bg-rose-500 animate-pulseDot'}`} />
            {connected ? '神殿已连接' : '重连中…'}
          </p>
        </div>
      </div>
      <p className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-600">对话</p>
      {chatAgents.map((a) => (
        <NavItem key={a.id} active={view.kind === 'chat' && view.agent === a.id} onClick={() => go({ kind: 'chat', agent: a.id })}>
          <Avatar role={a.role} size="sm" />
          <span className="flex-1 truncate">{a.name}</span>
          <StatusDot agent={a} />
        </NavItem>
      ))}
      <p className="px-3 pb-1 pt-5 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-600">宇宙</p>
      <NavItem active={view.kind === 'universe'} onClick={() => go({ kind: 'universe' })}>
        <Orbit className="h-[18px] w-[18px] text-cyan-300" /><span className="flex-1">总览</span>
        {working > 0 && <span className="chip bg-amber-400/15 text-amber-300">{working} 工作中</span>}
      </NavItem>
      <NavItem active={view.kind === 'messages'} onClick={() => go({ kind: 'messages' })}>
        <Waypoints className="h-[18px] w-[18px] text-fuchsia-300" /><span className="flex-1">消息流</span>
      </NavItem>
      <NavItem active={view.kind === 'settings'} onClick={() => go({ kind: 'settings' })}>
        <SlidersHorizontal className="h-[18px] w-[18px] text-amber-300" /><span className="flex-1">模型设置</span>
        {dir.agents.some((a) => a.runtime?.fallback_active) && <span className="chip bg-amber-400/15 text-amber-300">后备中</span>}
      </NavItem>
      <div className="mt-auto">
        <button onClick={logout} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm text-slate-500 transition hover:bg-white/5 hover:text-slate-300">
          <LogOut className="h-4 w-4" />退出登录
        </button>
      </div>
    </nav>
  )

  return (
    <PreviewContext.Provider value={setPreview}>
    <div className="flex h-full">
      <aside className="hidden w-64 shrink-0 border-r border-white/[0.06] bg-abyss-950/60 backdrop-blur-xl md:block">{sidebar}</aside>
      {menu && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setMenu(false)} />
          <aside className="relative h-full w-72 border-r border-white/10 bg-abyss-900 animate-rise">{sidebar}</aside>
        </div>
      )}
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-white/[0.06] px-3 py-2 md:hidden">
          <button className="btn-icon" onClick={() => setMenu(true)}>{menu ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}</button>
          <span className="font-display tracking-[0.2em] text-white">CTHULHU</span>
        </div>
        <div className="min-h-0 flex-1">
          {view.kind === 'chat' && active && (
            <Chat key={active.id} agent={active} turns={chats[active.id] ?? []} hasMore={!!more[active.id]} loadMore={loadMore}
              setTurns={(f) => setChats((c) => ({ ...c, [active.id]: f(c[active.id] ?? []) }))} streams={streams} />
          )}
          {view.kind === 'chat' && !active && (
            <div className="grid h-full place-items-center text-slate-500"><MessagesSquare className="h-8 w-8 animate-pulseDot" /></div>
          )}
          {view.kind === 'universe' && <Universe dir={dir} patrolTick={patrolTick} />}
          {view.kind === 'messages' && <Messages dir={dir} live={liveMsgs} />}
          {view.kind === 'settings' && <Settings dir={dir} />}
        </div>
      </main>
      {preview && (
        <>
          {/* Wide screens: a resizable-feel side column; narrow screens: a full overlay. */}
          <div className="hidden w-[min(46vw,760px)] shrink-0 lg:block">
            <FilePreview file={preview} onClose={closePreview} onOpen={setPreview} />
          </div>
          <div className="fixed inset-0 z-50 lg:hidden">
            <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={closePreview} />
            <div className="absolute inset-y-0 right-0 w-full max-w-2xl"><FilePreview file={preview} onClose={closePreview} onOpen={setPreview} /></div>
          </div>
        </>
      )}
    </div>
    </PreviewContext.Provider>
  )
}

function NavItem({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick}
      className={`flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm transition ${
        active ? 'bg-white/[0.08] text-white shadow-inner shadow-white/[0.02]' : 'text-slate-400 hover:bg-white/[0.04] hover:text-slate-200'
      }`}>
      {children}
    </button>
  )
}
