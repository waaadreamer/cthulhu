export type Role = 'azathoth' | 'shub' | 'yog' | 'ghroth' | 'old_one'

export interface Agent {
  id: string
  name: string
  role: Role
  galaxy_id: string
  parent_id: string | null
  desired: 'running' | 'stopped'
  task: string
  online: boolean
  state: string
  current: string | null
  context_fill: number | null
  queued: number
  memory?: { entries: number; tokens: number }
  runtime?: RuntimeStatus | null
  created_at: number
  updated_at: number
}

export interface RuntimeCfg {
  backend: 'claude' | 'codex'
  model: string
  effort?: string
  memory_model?: string
  memory_effort?: string
  fallback?: RuntimeCfg | null
}

export interface RuntimeStatus {
  backend: string
  model: string | null
  fallback_active: boolean
  config: RuntimeCfg
  default: RuntimeCfg
}

export interface Galaxy { id: string; name: string; online: boolean; created_at: number }

export interface Directory { agents: Agent[]; galaxies: Galaxy[]; gods?: Record<string, string | null> }

export interface Turn {
  id: string
  agent_id: string
  user_text: string
  reply: string
  activity: string[]
  status: 'queued' | 'running' | 'done' | 'interrupted' | 'failed'
  model?: string | null
  created_at: number
  updated_at: number
}

export interface Message {
  id: string
  sender: string
  receiver: string
  kind: string
  priority: number
  text: string
  state: string
  created_at: number
}

export interface Memory { id: number; text: string; created_at: number }
export interface Patrol { id: number; summary: string; created_at: number }

export class Unauthorized extends Error {}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api/${path}`, {
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
  shub:     { label: '黑山羊',   glyph: '❦', color: 'bg-emerald-500', ring: 'focus-within:ring-emerald-500/15', text: 'text-emerald-300', grad: 'from-emerald-500 to-teal-500' },
  yog:      { label: '尤格索托斯', glyph: '◉', color: 'bg-amber-500', ring: 'focus-within:ring-amber-500/15', text: 'text-amber-300', grad: 'from-amber-400 to-orange-500' },
  ghroth:   { label: '格赫罗斯', glyph: '☄', color: 'bg-rose-500', ring: 'focus-within:ring-rose-500/15', text: 'text-rose-300', grad: 'from-rose-500 to-pink-500' },
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
