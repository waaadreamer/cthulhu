import { AlertTriangle } from 'lucide-react'
import { modelLabel, RuntimeStatus } from '../api'

const STYLE: Record<string, string> = {
  claude: 'bg-orange-400/15 text-orange-200 ring-orange-400/30',
  codex: 'bg-sky-400/15 text-sky-200 ring-sky-400/30',
}

/** Which backend and model an agent is running on right now. */
export function RuntimeBadge({ runtime, compact }: { runtime?: RuntimeStatus | null; compact?: boolean }) {
  if (!runtime) return null
  const fb = runtime.fallback_active
  return (
    <span
      title={fb ? `主后端 ${runtime.config.backend}:${runtime.config.model} 暂时不可用，正在使用后备` : `${runtime.backend}:${runtime.model ?? ''}`}
      className={`chip ring-1 ${fb ? 'bg-amber-400/15 text-amber-200 ring-amber-400/40' : STYLE[runtime.backend] ?? STYLE.codex}`}
    >
      {fb && <AlertTriangle className="h-3 w-3" />}
      {!compact && <span className="opacity-70">{runtime.backend === 'claude' ? 'Claude' : 'Codex'}</span>}
      {modelLabel(runtime.model)}
      {fb && !compact && <span className="opacity-70">· 后备</span>}
    </span>
  )
}
