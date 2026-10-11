import { useEffect, useState } from 'react'
import { CircleAlert, CircleCheck, Loader2, Rocket } from 'lucide-react'
import { api, DeployJob, DeployStatus, Directory, timeLabel } from '../api'

const STATE: Record<string, { label: string; tone: string }> = {
  running: { label: '部署中', tone: 'bg-amber-400/15 text-amber-300' },
  restarting_temple: { label: '神殿重启中', tone: 'bg-amber-400/15 text-amber-300' },
  done: { label: '成功', tone: 'bg-emerald-400/15 text-emerald-300' },
  failed: { label: '失败', tone: 'bg-rose-400/15 text-rose-300' },
}

/** Versions of the temple and every galaxy, a one-click deploy of origin/main, and live job progress. */
export function Deploy({ dir, tick }: { dir: Directory; tick: number }) {
  const [status, setStatus] = useState<DeployStatus | null>(null)
  const [latest, setLatest] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = () => api<DeployStatus>('deploy').then(setStatus).catch((e) => setError(e.message))
  useEffect(() => { load() }, [tick])
  useEffect(() => { api<{ revision: string }>('deploy_latest').then((r) => setLatest(r.revision)).catch(() => {}) }, [tick])

  const job = status?.jobs[0]
  const active = job && (job.state === 'running' || job.state === 'restarting_temple')
  const deploy = async (galaxies: string[] | 'all') => {
    if (!confirm(`把 ${latest?.slice(0, 12) ?? '最新 main'} 部署到${galaxies === 'all' ? '中心神殿和全部星系' : ` ${galaxies.join('、')}`}？\n会先排空（等当前回合结束）再重启。`)) return
    setBusy(true); setError(null)
    try { await api('deploy', { galaxies }); await load() } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const short = (r?: string) => (r ? r.slice(0, 7) : '—')
  const stale = (r?: string) => !!latest && !!r && r !== latest

  return (
    <section>
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-500">部署</h2>
        <span className="text-xs text-slate-600">GitHub main：<span className="font-mono">{short(latest ?? undefined)}</span></span>
        <button onClick={() => deploy('all')} disabled={busy || !!active || !latest}
          className="ml-auto flex items-center gap-1.5 rounded-xl bg-gradient-to-br from-violet-500 to-fuchsia-500 px-3 py-1.5 text-sm text-white shadow-lg transition hover:brightness-110 disabled:opacity-30">
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Rocket className="h-4 w-4" />}全部部署
        </button>
      </div>
      {error && <p className="mb-3 rounded-xl border border-rose-400/20 bg-rose-400/[0.06] p-3 text-sm text-rose-200">{error}</p>}
      <div className="glass overflow-hidden rounded-2xl">
        <table className="w-full text-sm">
          <tbody className="divide-y divide-white/[0.04]">
            <tr>
              <td className="px-4 py-2.5 text-slate-200">中心神殿</td>
              <td className={`px-4 font-mono text-xs ${stale(status?.temple.revision) ? 'text-amber-300' : 'text-slate-400'}`}>{short(status?.temple.revision)}</td>
              <td />
            </tr>
            {dir.galaxies.map((g) => (
              <tr key={g.id}>
                <td className="px-4 py-2.5 text-slate-200">{g.name} <span className="font-mono text-xs text-slate-600">{g.id}</span></td>
                <td className={`px-4 font-mono text-xs ${stale(g.revision) ? 'text-amber-300' : 'text-slate-400'}`}>
                  {!g.online ? <span className="text-slate-600">离线</span> : g.deployable ? short(g.revision)
                    : <span className="text-amber-300/80" title="旧版本不支持远程部署，需要现场升级一次">需现场升级</span>}
                  {g.draining && <span className="ml-2 text-amber-300">排空中</span>}
                </td>
                <td className="px-4 text-right">
                  {g.online && g.deployable && stale(g.revision) && (
                    <button onClick={() => deploy([g.id])} disabled={busy || !!active} className="text-xs text-cyan-300 hover:underline disabled:opacity-30">部署</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {job && <JobView job={job} />}
      {job?.state === 'failed' && job.groups.some((g) => g.recovery_required) && (
        <div className="mt-3 text-sm text-amber-300">
          <p>回滚尚未确认，相关星系保持排空。恢复不会重新发布失败版本。</p>
          <button disabled={busy} className="mt-2 text-cyan-300 disabled:opacity-30" onClick={async () => {
            setBusy(true); setError(null)
            try { await api('deploy', { recover_job_id: job.id }); await load() }
            catch (e) { setError((e as Error).message) } finally { setBusy(false) }
          }}>重新确认回滚</button>
        </div>
      )}
    </section>
  )
}

function JobView({ job }: { job: DeployJob }) {
  const st = STATE[job.state] ?? STATE.running
  const live = job.state === 'running' || job.state === 'restarting_temple'
  return (
    <div className="mt-3 rounded-2xl border border-white/[0.06] bg-black/20 p-4 text-sm">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {live ? <Loader2 className="h-4 w-4 animate-spin text-amber-300" /> : job.state === 'done'
          ? <CircleCheck className="h-4 w-4 text-emerald-400" /> : <CircleAlert className="h-4 w-4 text-rose-300" />}
        <span className="text-slate-200">最近一次部署</span>
        <span className={`chip ${st.tone}`}>{st.label}</span>
        <span className="font-mono text-xs text-slate-500">{job.revision.slice(0, 12)}</span>
        <span className="text-xs text-slate-500">· {job.by} · {timeLabel(job.created_at)}</span>
      </div>
      {job.reason && <p className="mb-2 text-xs text-slate-400">{job.reason}</p>}
      <ol className="max-h-64 space-y-1 overflow-y-auto font-mono text-xs text-slate-400">
        {job.steps.map((s, i) => (
          <li key={i}><span className="text-slate-600">{timeLabel(s.at)}</span> {s.galaxy && <span className="text-cyan-300/80">[{s.galaxy}]</span>} {s.text}</li>
        ))}
      </ol>
    </div>
  )
}
