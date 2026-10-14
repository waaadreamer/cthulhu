import { useEffect, useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import { api, Approval, timeLabel } from '../api'

/** Azathoth's pending sudo requests. Each one blocks its tool call until the owner decides here. */
export function SudoApprovals({ tick }: { tick: number }) {
  const [pending, setPending] = useState<Approval[]>([])
  const [busy, setBusy] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  useEffect(() => { api<Approval[]>('approvals?state=pending').then(setPending).catch(() => {}) }, [tick])
  const a = pending[pending.length - 1]
  if (!a) return null
  const decide = async (approve: boolean) => {
    setBusy(a.id); setError('')
    try {
      await api('approvals', { id: a.id, approve, note })
      setPending((p) => p.filter((x) => x.id !== a.id)); setNote('')
    } catch (e) { setError((e as Error).message); setPending((p) => p.filter((x) => x.id !== a.id)) }
    finally { setBusy('') }
  }
  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-black/70 p-4 backdrop-blur-sm" role="dialog" aria-modal="true"
      aria-labelledby="sudo-title">
      <div className="w-full max-w-xl rounded-2xl border border-rose-500/30 bg-abyss-900 p-5 shadow-2xl animate-rise">
        <div className="mb-3 flex items-center gap-2">
          <ShieldAlert className="h-5 w-5 text-rose-300" />
          <h2 id="sudo-title" className="font-semibold text-white">阿撒托斯请求以 root 执行</h2>
          {pending.length > 1 && <span className="chip bg-rose-500/15 text-rose-300">还有 {pending.length - 1} 条</span>}
          <span className="ml-auto text-xs text-slate-500">{timeLabel(a.created_at)}</span>
        </div>
        <pre className="mb-3 max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-black/40 p-3 font-mono text-sm text-rose-100">{a.command}</pre>
        <dl className="mb-4 grid grid-cols-[4rem_1fr] gap-x-3 gap-y-1 text-sm text-slate-300">
          <dt className="text-slate-500">理由</dt><dd className="whitespace-pre-wrap break-words">{a.reason || '（未说明）'}</dd>
          <dt className="text-slate-500">目录</dt><dd className="break-all font-mono text-xs">{a.cwd || '-'}</dd>
          <dt className="text-slate-500">超时</dt><dd>{Math.round(a.timeout)} 秒；30 分钟内不处理自动作废</dd>
        </dl>
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="给阿撒托斯的说明（可空）"
          className="mb-3 w-full rounded-lg border border-white/10 bg-black/20 p-2 text-sm" />
        {error && <p role="alert" className="mb-2 text-xs text-rose-300">{error}</p>}
        <div className="flex justify-end gap-2">
          <button disabled={!!busy} onClick={() => decide(false)}
            className="rounded-lg bg-white/[0.06] px-4 py-2 text-sm text-slate-200 hover:bg-white/10 disabled:opacity-40">拒绝</button>
          <button disabled={!!busy} onClick={() => decide(true)}
            className="rounded-lg bg-rose-500/80 px-4 py-2 text-sm font-medium text-white hover:bg-rose-500 disabled:opacity-40">批准执行</button>
        </div>
      </div>
    </div>
  )
}
