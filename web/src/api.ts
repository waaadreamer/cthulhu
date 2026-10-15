export type Role = 'azathoth' | 'nyarlathotep' | 'old_one'

export interface Agent {
  id: string
  name: string
  role: Role
  galaxy_id: string
  parent_id: string | null
  task_id?: string | null
  desired: 'running' | 'stopped'
  stopped_by?: string | null
  task: string
  online: boolean
  state: string
  current: string | null
  context_fill: number | null
  queued: number
  memory?: { entries: number; tokens: number; updated_at?: number }
  runtime?: RuntimeStatus | null
  admission?: { path: string; config?: AdmissionConfig; error?: string } | null
  created_at: number
  updated_at: number
}

export interface RuntimeCfg {
  backend: 'claude' | 'codex' | 'galaxy_api'
  model: string
  provider?: string
  service_tier?: string
  effort?: string
  memory_model?: string
  memory_effort?: string
  fallback?: RuntimeCfg | null
}

export interface RuntimeStatus {
  recovery_policy?: { retries: number; fallback_hold_seconds: number }
  recovery?: { phase: string; attempt: number; retry: number; retries: number; source: string; fallback: boolean; error?: string }
  api_result?: { provider?: string; requested_model?: string; requested_tier?: string; returned_model?: string; returned_tier?: string }
  backend: string
  model: string | null
  fallback_active: boolean
  config: RuntimeCfg
  default: RuntimeCfg
  pending?: RuntimeCfg | null
}

export interface Galaxy { id: string; name: string; online: boolean; created_at: number; revision?: string; deployable?: boolean; draining?: boolean; hub?: boolean; spawn?: boolean }

export interface DeployJob {
  id: string; by: string; reason: string; revision: string; created_at: number; finished_at: number | null
  state: 'running' | 'restarting_temple' | 'done' | 'failed'
  groups: { galaxies: string[]; temple: boolean; state: string; error?: string | null; recovery_required?: boolean }[]
  steps: { at: number; galaxy: string | null; text: string }[]
}
export interface DeployStatus { temple: { revision: string; host: string }; jobs: DeployJob[]; requests: { id: string; galaxies: unknown; reason: string; state: string }[] }

export interface Directory { agents: Agent[]; galaxies: Galaxy[]; gods?: Record<string, string | null> }

export interface Turn {
  report?: { facts: string; decision?: string; period?: number; event_ids: number[]; actions: { at: number; method: string; params: unknown; result: unknown; error?: string; summary: string }[] } | null
  channel_receipt?: { state?: 'sending' | 'sent' | 'ambiguous' | 'failed'; message_id?: string } | null
  id: string
  agent_id: string
  user_text: string
  reply: string
  activity: string[]
  status: 'queued' | 'running' | 'done' | 'interrupted' | 'failed'
  model?: string | null
  origin?: string | null
  task_id?: string | null
  task_title?: string | null
  task_state?: string | null
  kind?: 'progress' | 'blocked' | 'summary' | 'notice' | null
  created_at: number
  updated_at: number
}

export type TaskState = 'assigning' | 'awaiting_probe' | 'running' | 'review' | 'suspended' | 'closed' | 'cancelled' | 'assign_failed'
export interface TaskSample { ts: number; stage: number; value: number | null; error?: string | null; baseline?: boolean; final?: boolean }
export interface TaskEvent { at: number; kind: string; text: string }
export interface Probe { stage: number; command: string; rationale: string; verdict: string; reason: string; registered_at: number; baseline: number; stage_note?: string }
/** Index row from the temple (task list). */
export interface Task {
  id: string; short_name: string; galaxy_id: string; description: string; acceptance: string; state: TaskState
  executor?: string | null; executor_name?: string | null; line?: string; summary?: string
  created_at: number; updated_at: number; closed_at?: number | null
}
/** Full detail from the task's hub. */
export interface TaskDetail extends Task {
  details?: string
  data?: {
    reused?: boolean; reason?: string; probe?: Probe | null; probe_history?: (Probe & { to?: number })[]
    samples?: TaskSample[]; events?: TaskEvent[]; respawns?: number; blocker?: { text: string; at: number } | null
    checkpoint?: { n: number; next_at: number | null; paused: boolean; stall_streak: number }
    submissions?: { at: number; evidence: string; verdict: string | null; note: string | null }[]
    last_summary?: string; last_pace?: string; executors?: { agent_id: string; from: number; to: number | null; reason: string }[]
  }
  messages?: { id: string; sender: string; sender_name: string; receiver: string; kind: string; priority: number; text: string; created_at: number }[]
}

export const TASK_STATE: Record<TaskState, { label: string; chip: string }> = {
  assigning: { label: '派单中', chip: 'bg-slate-400/15 text-slate-300' },
  awaiting_probe: { label: '待登记探针', chip: 'bg-amber-400/15 text-amber-300' },
  running: { label: '进行中', chip: 'bg-sky-400/15 text-sky-300' },
  review: { label: '待审核', chip: 'bg-violet-400/15 text-violet-300' },
  suspended: { label: '已挂起', chip: 'bg-rose-500/15 text-rose-300' },
  closed: { label: '已完成', chip: 'bg-emerald-400/15 text-emerald-300' },
  cancelled: { label: '已取消', chip: 'bg-slate-500/15 text-slate-400' },
  assign_failed: { label: '派单失败', chip: 'bg-rose-500/15 text-rose-300' },
}

export interface Approval {
  id: string; command: string; reason: string; cwd?: string | null; timeout: number
  state: 'pending' | 'approved' | 'rejected' | 'expired' | 'done' | 'failed'
  note?: string | null; result?: string | null; created_at: number; decided_at?: number | null; finished_at?: number | null
}

export interface Message {
  task_title?: string | null
  id: string
  task_id?: string | null
  status?: string | null
  sender: string
  receiver: string
  kind: string
  priority: number
  text: string
  state: string
  created_at: number
}

export interface Memory { id: number; text: string; source?: string | null; created_at: number; updated_at?: number | null }

export class Unauthorized extends Error {}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  // Relative, so the UI works both at / and under a path prefix such as /cthulhu/.
  const r = await fetch(`api/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'content-type': 'application/json', 'x-cthulhu': '1' },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  })
  const data = await r.json().catch(() => ({}))
  if (r.status === 401) throw new Unauthorized(data.error)
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`)
  return data as T
}

export const ROLE_META: Record<Role, { label: string; glyph: string; color: string; ring: string; text: string; grad: string }> = {
  azathoth: { label: '阿撒托斯', glyph: '✺', color: 'bg-violet-500', ring: 'focus-within:ring-violet-500/15', text: 'text-violet-300', grad: 'from-violet-500 to-fuchsia-500' },
  nyarlathotep: { label: '奈亚拉托提普', glyph: '✧', color: 'bg-indigo-500', ring: 'focus-within:ring-indigo-500/15', text: 'text-indigo-300', grad: 'from-indigo-500 to-cyan-500' },
  old_one:  { label: '旧日支配者', glyph: '◈', color: 'bg-cyan-500', ring: 'focus-within:ring-cyan-500/15', text: 'text-cyan-300', grad: 'from-cyan-500 to-sky-500' },
}

export function timeLabel(t: number) {
  const d = new Date(t * 1000)
  const now = new Date()
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (d.toDateString() === now.toDateString()) return hm
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
}

export function ago(t: number) {
  const s = Date.now() / 1000 - t
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return `${Math.floor(s / 86400)} 天前`
}

export function tokens(n: number) {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

export function modelLabel(model?: string | null) {
  if (!model) return '默认模型'
  const m = model.replace(/^claude-/, '').replace(/-(\d)-(\d)$/, ' $1.$2').replace(/^gpt-/, 'GPT-')
  return m.charAt(0).toUpperCase() + m.slice(1)
}

// Nyarl's own admission config (admission.json); Nyarl maintains it, the overview only shows it.
export interface AdmissionConfig {
  version?: unknown
  default?: unknown
  contacts?: unknown
  [key: string]: unknown
}
