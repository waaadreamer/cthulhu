import { useState } from 'react'
import { Loader2, OctagonX, Play } from 'lucide-react'
import { Agent, api } from '../api'

// Owner-only force stop / resume of any agent, gods included. The temple enforces who may call it.
export function ForceStop({ agent, compact }: { agent: Agent; compact?: boolean }) {
  const [busy, setBusy] = useState(false)
  const stopped = agent.desired === 'stopped'
  const act = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (busy) return
    if (!stopped && !confirm(`强制停止${agent.name}？\n当前回合立即中断，模型进程被终止；排队中的消息作废，恢复前拒收一切消息。`)) return
    setBusy(true)
    try { await api(stopped ? 'resume' : 'force_stop', { agent_id: agent.id }) }
    catch (err) { alert((err as Error).message) }
    finally { setBusy(false) }
  }
  const label = stopped ? '恢复' : '强制停止'
  const icon = busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : stopped ? <Play className="h-3.5 w-3.5" /> : <OctagonX className="h-3.5 w-3.5" />
  const tone = stopped ? 'border-emerald-400/30 text-emerald-300 hover:bg-emerald-400/10' : 'border-rose-400/30 text-rose-300 hover:bg-rose-400/10'
  return (
    <span role="button" tabIndex={0} title={label} aria-label={label} onClick={act}
      onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && act(e as unknown as React.MouseEvent)}
      className={`inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-lg border px-2 py-1 text-xs transition ${tone}`}>
      {icon}{!compact && label}
    </span>
  )
}
