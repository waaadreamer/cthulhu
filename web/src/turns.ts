import { Turn } from './api'

export function mergeTurns(current: Turn[], incoming: Turn[]): Turn[] {
  const rows = new Map(current.map((t) => [t.id, t]))
  for (const t of incoming) {
    const old = rows.get(t.id)
    if (!old || t.updated_at >= old.updated_at) rows.set(t.id, t)
  }
  return [...rows.values()].sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id))
}
