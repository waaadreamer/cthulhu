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
  const recovery = runtime.recovery
  const recovering = recovery && ['running', 'checking', 'retrying', 'switching'].includes(recovery.phase) && recovery.attempt > 1
  const detail = api ? `请求 ${api.requested_model ?? runtime.model}/${api.requested_tier ?? 'default'} → 实际 ${api.returned_model ?? '未知'}/${api.returned_tier ?? '未知'} · provider ${api.provider}` : `${runtime.backend}:${runtime.model ?? ''}`
  return (
    <span
      title={[detail, fb ? '正在使用已配置的后备源' : '', recovery ? `第 ${recovery.attempt} 次尝试 · ${recovery.source} · ${recovery.phase}${recovery.error ? ` · ${recovery.error}` : ''}` : ''].filter(Boolean).join('\n')}
      className={`chip ring-1 ${fb ? 'bg-amber-400/15 text-amber-200 ring-amber-400/40' : STYLE[runtime.backend] ?? STYLE.codex}`}
    >
      {fb && <AlertTriangle className="h-3 w-3" />}
      {!compact && <span className="opacity-70">{runtime.backend === 'claude' ? 'Claude' : runtime.backend === 'galaxy_api' ? '星系 API' : '个人 Codex'}</span>}
      {modelLabel(runtime.model)}
      {runtime.api_result?.returned_tier && !compact && <span className="opacity-70">· 实际 {runtime.api_result.returned_tier}</span>}
      {fb && !compact && <span className="opacity-70">· 后备</span>}
      {recovering && <span>· 重试 {recovery.retry}/{recovery.retries}</span>}
      {recovery?.phase === 'failed' && <span>· 恢复失败</span>}
    </span>
  )
}
