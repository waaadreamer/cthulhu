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
  const api = runtime.api_result
  const detail = api ? `请求 ${api.requested_model ?? runtime.model}/${api.requested_tier ?? 'default'} → 实际 ${api.returned_model ?? '未知'}/${api.returned_tier ?? '未知'} · provider ${api.provider}` : `${runtime.backend}:${runtime.model ?? ''}`
  return (
    <span
      title={fb ? `主后端 ${runtime.config.backend}:${runtime.config.model} 暂时不可用，正在使用后备` : detail}
      className={`chip ring-1 ${fb ? 'bg-amber-400/15 text-amber-200 ring-amber-400/40' : STYLE[runtime.backend] ?? STYLE.codex}`}
    >
      {fb && <AlertTriangle className="h-3 w-3" />}
      {!compact && <span className="opacity-70">{runtime.backend === 'claude' ? 'Claude' : runtime.backend === 'galaxy_api' ? '星系 API' : '个人 Codex'}</span>}
      {modelLabel(runtime.model)}
      {runtime.api_result?.returned_tier && !compact && <span className="opacity-70">· 实际 {runtime.api_result.returned_tier}</span>}
      {fb && !compact && <span className="opacity-70">· 后备</span>}
    </span>
  )
}
