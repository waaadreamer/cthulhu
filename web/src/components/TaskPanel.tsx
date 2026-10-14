import { useEffect, useState } from 'react'
import { api, Directory, TaskDetail, TASK_STATE, timeLabel } from '../api'

/** One task as its hub sees it: probe, checkpoints, timeline, submissions; with the owner's buttons. */
export function TaskPanel({ taskId, dir, tick }: { taskId: string; dir: Directory; tick: number }) {
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const load = () => api<TaskDetail>(`task?task_id=${encodeURIComponent(taskId)}`).then(setTask)
  useEffect(() => { load().catch((e) => setError(e.message)) }, [taskId, tick])
  if (!task) return error ? <p role="alert" className="mb-4 text-sm text-rose-300">{error}</p> : null

  const name = (id?: string | null) => dir.agents.find((a) => a.id === id)?.name ?? id ?? '-'
  const act = async (body: Record<string, unknown>) => {
    setBusy(true); setError('')
    try { await api('task', { task_id: taskId, ...body }); await load() }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const d = task.data ?? {}
  const cp = d.checkpoint
  const st = TASK_STATE[task.state] ?? TASK_STATE.running
  const open = !['closed', 'cancelled', 'assign_failed'].includes(task.state)
  const galaxy = dir.galaxies.find((g) => g.id === task.galaxy_id)
  const btn = 'rounded-lg px-3 py-1.5 text-xs transition disabled:opacity-40'
  const stageSamples = (d.samples ?? []).filter((s) => s.stage === d.probe?.stage)

  return (
    <section data-task-panel className="mb-6 space-y-4 rounded-2xl border border-white/[0.06] bg-white/[0.025] p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`chip ${st.chip}`}>{st.label}</span>
        {!!cp?.stall_streak && open && <span className="chip bg-rose-500/15 text-rose-300">连续 {cp.stall_streak} 个检查点无进展</span>}
        {d.blocker && <span className="chip bg-amber-400/15 text-amber-300">阻塞中</span>}
        {!!d.respawns && <span className="chip bg-rose-500/15 text-rose-300">已替换 {d.respawns} 次</span>}
        <span className="ml-auto text-xs text-slate-500">
          CP{cp?.n ?? 0} · 下次检查 {cp?.next_at && !cp.paused && open ? timeLabel(cp.next_at) : '-'}
        </span>
      </div>
      {task.details && <p className="text-xs text-amber-300">{task.details}</p>}
      <dl className="grid grid-cols-[5rem_1fr] gap-x-3 gap-y-1.5 text-slate-300">
        <dt className="text-slate-500">星系</dt><dd>{galaxy?.name ?? task.galaxy_id}</dd>
        <dt className="text-slate-500">执行者</dt>
        <dd>{task.executor_name ?? name(task.executor)}{d.reason && <span className="text-slate-500">（{d.reused ? '复用' : '新建'}：{d.reason}）</span>}</dd>
        <dt className="text-slate-500">验收标准</dt><dd className="whitespace-pre-wrap break-words">{task.acceptance || '-'}</dd>
        <dt className="text-slate-500">探针</dt>
        <dd className="break-all font-mono text-xs">
          {d.probe ? <>S{d.probe.stage} · {d.probe.command} <span className="font-sans text-slate-500">（{d.probe.verdict}）</span></>
            : <span className="text-rose-300">未登记</span>}
        </dd>
        {(d.last_summary || task.summary) && <><dt className="text-slate-500">最新总结</dt><dd className="break-words">{d.last_summary || task.summary}</dd></>}
        {d.blocker && <><dt className="text-slate-500">阻塞</dt><dd className="whitespace-pre-wrap break-words text-amber-200">{d.blocker.text}</dd></>}
      </dl>

      {stageSamples.length > 0 && <Metric samples={stageSamples} />}

      {(d.submissions?.length ?? 0) > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold text-slate-400">提交</h3>
          <ul className="space-y-2 text-xs text-slate-400">
            {d.submissions!.map((s, i) => (
              <li key={i}>
                <span className="text-slate-500">{timeLabel(s.at)} · </span>
                <span className={s.verdict === 'passed' ? 'text-emerald-300' : s.verdict === 'rejected' ? 'text-rose-300' : 'text-violet-300'}>
                  {s.verdict === 'passed' ? '通过' : s.verdict === 'rejected' ? '驳回' : '待审核'}</span>
                {s.note ? `（${s.note}）` : ''}
                <p className="mt-0.5 whitespace-pre-wrap break-words text-slate-300">{s.evidence}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {open && (
        <div className="flex flex-wrap gap-2">
          {task.state === 'review' && <button disabled={busy} className={`${btn} bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30`}
            onClick={() => act({ action: 'close', note: '主人验收' })}>验收通过</button>}
          {['review', 'suspended', 'running', 'awaiting_probe'].includes(task.state) && <button disabled={busy}
            className={`${btn} bg-amber-400/20 text-amber-200 hover:bg-amber-400/30`}
            onClick={() => { const r = prompt('给执行者的指令'); if (r) act({ action: 'continue', instruction: r }) }}>
            {task.state === 'review' ? '驳回并继续' : '下指令并继续'}</button>}
          <button disabled={busy} className={`${btn} bg-rose-500/20 text-rose-200 hover:bg-rose-500/30`}
            onClick={() => { if (confirm('取消这个任务？')) act({ action: 'cancel', reason: '主人取消' }) }}>取消任务</button>
        </div>
      )}
      {error && <p role="alert" className="text-xs text-rose-300">{error}</p>}

      {(d.events?.length ?? 0) > 0 && (
        <details>
          <summary className="cursor-pointer text-xs font-semibold text-slate-400">记录（{d.events!.length}）</summary>
          <ol className="mt-2 space-y-1 border-l border-white/10 pl-3 text-xs text-slate-400">
            {d.events!.slice().reverse().map((e, i) => (
              <li key={i}><span className="text-slate-500">{timeLabel(e.at)} · {e.kind}</span> {e.text}</li>
            ))}
          </ol>
        </details>
      )}
    </section>
  )
}

/** Probe values of the current stage: a sparkline with failed samples marked, plus the latest values. */
function Metric({ samples }: { samples: NonNullable<TaskDetail['data']>['samples'] & object }) {
  const nums = samples.map((s) => s.value).filter((v): v is number => v !== null)
  if (!nums.length) return null
  const lo = Math.min(...nums), hi = Math.max(...nums)
  const W = 240, H = 40, n = Math.max(samples.length - 1, 1)
  const x = (i: number) => (i / n) * W
  const y = (v: number) => (hi === lo ? H / 2 : H - 4 - ((v - lo) / (hi - lo)) * (H - 8))
  const path = samples.map((s, i) => (s.value === null ? '' : `${x(i)},${y(s.value)}`)).filter(Boolean).join(' ')
  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold text-slate-400">指标</h3>
      <div className="flex flex-wrap items-center gap-4">
        <svg viewBox={`-4 0 ${W + 8} ${H}`} className="h-10 w-60 shrink-0" role="img" aria-label="探针指标走势">
          <polyline points={path} fill="none" stroke="currentColor" strokeWidth="1.5" className="text-cyan-300" />
          {samples.map((s, i) => s.value === null
            ? <circle key={i} cx={x(i)} cy={H / 2} r="2.5" className="fill-rose-400" />
            : <circle key={i} cx={x(i)} cy={y(s.value)} r="2" className="fill-cyan-300" />)}
        </svg>
        <ol className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-xs">
          {samples.slice(-8).map((s, i) => (
            <li key={i} title={s.error ?? undefined} className={s.value === null ? 'text-rose-300' : 'text-slate-400'}>
              {timeLabel(s.ts)} {s.value ?? 'n/a'}
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}
