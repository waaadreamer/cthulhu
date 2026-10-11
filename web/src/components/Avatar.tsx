import { Agent, Role, ROLE_META } from '../api'

export function Avatar({ role, size = 'md', live }: { role: Role; size?: 'sm' | 'md' | 'lg'; live?: Agent }) {
  const m = ROLE_META[role]
  const dim = { sm: 'h-7 w-7 text-sm', md: 'h-9 w-9 text-base', lg: 'h-12 w-12 text-xl' }[size]
  return (
    <div className="relative shrink-0">
      <div className={`grid ${dim} place-items-center rounded-xl bg-gradient-to-br ${m.grad} text-white shadow-md`}>{m.glyph}</div>
      {live && <StatusDot agent={live} className="absolute -bottom-0.5 -right-0.5 ring-2 ring-abyss-900" />}
    </div>
  )
}

export function StatusDot({ agent, className = '' }: { agent: Agent; className?: string }) {
  const s = stateOf(agent)
  return <span className={`block h-2.5 w-2.5 rounded-full ${s.dot} ${s.pulse ? 'animate-pulseDot' : ''} ${className}`} title={s.label} />
}

export function stateOf(a: Agent) {
  if (a.desired === 'stopped') return { label: '已终止', dot: 'bg-slate-600', chip: 'bg-slate-500/15 text-slate-400', pulse: false }
  if (!a.online) {
    if (a.state === 'crashed') return { label: '崩溃重启中', dot: 'bg-rose-500', chip: 'bg-rose-500/15 text-rose-300', pulse: true }
    return { label: '离线', dot: 'bg-slate-500', chip: 'bg-slate-500/15 text-slate-400', pulse: false }
  }
  if (a.state === 'working') return { label: '工作中', dot: 'bg-amber-400', chip: 'bg-amber-400/15 text-amber-300', pulse: true }
  if (a.state === 'starting') return { label: '启动中', dot: 'bg-sky-400', chip: 'bg-sky-400/15 text-sky-300', pulse: true }
  return { label: '空闲', dot: 'bg-emerald-400', chip: 'bg-emerald-400/15 text-emerald-300', pulse: false }
}
