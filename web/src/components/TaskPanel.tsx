import { useEffect, useState } from 'react'
import { api, Directory, Task, TASK_STATE, timeLabel } from '../api'

/** One task's contract and discipline record, with the owner's buttons. */
export function TaskPanel({ taskId, dir, tick }: { taskId: string; dir: Directory; tick: number }) {
  const [task, setTask] = useState<Task | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { api<Task>(`task?task_id=${encodeURIComponent(taskId)}`).then(setTask).catch((e) => setError(e.message)) }, [taskId, tick])
  if (!task) return error ? <p role="alert" className="mb-4 text-sm text-rose-300">{error}</p> : null

  const name = (id?: string | null) => (id === 'owner' ? '主人' : dir.agents.find((a) => a.id === id)?.name ?? id ?? '-')
  const act = async (body: Record<string, unknown>) => {
    setBusy(true); setError('')
    try { await api('task', { task_id: taskId, ...body }); setTask(await api<Task>(`task?task_id=${encodeURIComponent(taskId)}`)) }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const d = task.data ?? {}
  const st = TASK_STATE[task.state] ?? TASK_STATE.active
  const pending = d.extensions?.find((e) => e.state === 'pending' && e.approver === 'owner')
  const verdict = task.state === 'submitted' && (d.owner_verdict || !dir.agents.some((a) => a.id === task.executor && a.role === 'old_one'))
  const open = ['active', 'blocked_on_owner', 'submitted'].includes(task.state)
  const overdue = open && task.deadline && task.deadline * 1000 < Date.now()
  const btn = 'rounded-lg px-3 py-1.5 text-xs transition disabled:opacity-40'

  return (
    <section data-task-panel className="mb-6 space-y-4 rounded-2xl border border-white/[0.06] bg-white/[0.025] p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`chip ${st.chip}`}>{st.label}</span>
        {d.quiet && <span className="chip bg-slate-500/15 text-slate-400">告警已暂停</span>}
        {!!d.misses && open && <span className="chip bg-rose-500/15 text-rose-300">连续 {d.misses} 个检查点无进展</span>}
        <span className="ml-auto text-xs text-slate-500">下次检查 {d.next_check && task.state === 'active' ? timeLabel(d.next_check) : '-'}</span>
      </div>
      <dl className="grid grid-cols-[5rem_1fr] gap-x-3 gap-y-1.5 text-slate-300">
        <dt className="text-slate-500">执行者</dt><dd>{name(task.executor)}</dd>
        <dt className="text-slate-500">管理者</dt><dd>{name(task.manager)} · 创建 {name(task.creator)}</dd>
        <dt className="text-slate-500">截止</dt><dd className={overdue ? 'text-rose-300' : ''}>{task.deadline ? timeLabel(task.deadline) : '-'}{overdue ? '（已超时）' : ''}</dd>
        <dt className="text-slate-500">验收标准</dt><dd className="whitespace-pre-wrap break-words">{task.acceptance || '-'}</dd>
        <dt className="text-slate-500">探针</dt><dd className="break-all font-mono text-xs">{task.probe || <span className="text-rose-300">未登记</span>}</dd>
      </dl>

      {(d.samples?.length ?? 0) > 0 && <Metric samples={d.samples!} />}

      {(d.extensions?.length ?? 0) > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold text-slate-400">延期</h3>
          <ul className="space-y-1 text-xs text-slate-400">
            {d.extensions!.map((e, i) => (
              <li key={i}>第 {i + 1} 次 · {timeLabel(e.at)} 申请至 {timeLabel(e.until)} · {e.reason} · 审批 {name(e.approver)} ·{' '}
                <span className={e.state === 'approved' ? 'text-emerald-300' : e.state === 'pending' ? 'text-amber-300' : 'text-rose-300'}>
                  {{ approved: '批准', denied: '拒绝', refused: '程序拒绝', pending: '待批' }[e.state]}</span>{e.decision ? `（${e.decision}）` : ''}</li>
            ))}
          </ul>
        </div>
      )}

      {(pending || verdict || task.state === 'blocked_on_owner' || open) && (
        <div className="flex flex-wrap gap-2">
          {pending && <>
            <button disabled={busy} className={`${btn} bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30`} onClick={() => act({ action: 'extension', approve: true })}>批准延期至 {timeLabel(pending.until)}</button>
            <button disabled={busy} className={`${btn} bg-rose-500/20 text-rose-200 hover:bg-rose-500/30`} onClick={() => act({ action: 'extension', approve: false, reason: prompt('拒绝理由') ?? '' })}>拒绝延期</button>
          </>}
          {verdict && <>
            <button disabled={busy} className={`${btn} bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30`} onClick={() => act({ action: 'verdict', passed: true })}>验收通过</button>
            <button disabled={busy} className={`${btn} bg-rose-500/20 text-rose-200 hover:bg-rose-500/30`} onClick={() => { const r = prompt('驳回理由'); if (r) act({ action: 'verdict', passed: false, reason: r }) }}>驳回</button>
          </>}
          {task.state === 'blocked_on_owner' && (
            <button disabled={busy} className={`${btn} bg-amber-400/20 text-amber-200 hover:bg-amber-400/30`} onClick={() => act({ action: 'resolve', note: prompt('给执行者的说明（可空）') ?? '' })}>已解决，恢复计时</button>
          )}
          {open && (
            <button disabled={busy} className={`${btn} bg-white/[0.06] text-slate-300 hover:bg-white/10`} onClick={() => act({ action: 'quiet', quiet: !d.quiet })}>{d.quiet ? '恢复告警' : '暂停告警'}</button>
          )}
        </div>
      )}
      {error && <p role="alert" className="text-xs text-rose-300">{error}</p>}

      {(d.events?.length ?? 0) > 0 && (
        <details>
          <summary className="cursor-pointer text-xs font-semibold text-slate-400">记录（{d.events!.length}）</summary>
          <ol className="mt-2 space-y-1 border-l border-white/10 pl-3 text-xs text-slate-400">
            {d.events!.slice().reverse().map((e, i) => (
              <li key={i}><span className="text-slate-500">{timeLabel(e.at)} · {name(e.by === 'temple' ? '' : e.by) || '神殿'}</span> {e.text}</li>
            ))}
          </ol>
        </details>
      )}
    </section>
  )
}

/** Probe history: a sparkline with stalled checkpoints marked, plus the latest values. */
function Metric({ samples }: { samples: NonNullable<Task['data']>['samples'] & object }) {
  const vals = samples.map((s) => s.value)
  const nums = vals.filter((v): v is number => v !== null)
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
          {samples.map((s, i) => s.value === null || s.progress === false
            ? <circle key={i} cx={x(i)} cy={s.value === null ? H / 2 : y(s.value)} r="2.5" className="fill-rose-400" />
            : <circle key={i} cx={x(i)} cy={y(s.value)} r="2" className="fill-cyan-300" />)}
        </svg>
        <ol className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-xs">
          {samples.slice(-8).map((s, i) => (
            <li key={i} title={s.note} className={s.progress === false || s.value === null ? 'text-rose-300' : 'text-slate-400'}>
              {timeLabel(s.at)} {s.value ?? 'n/a'}
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}
