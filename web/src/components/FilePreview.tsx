import { useEffect, useState } from 'react'
import { ArrowLeft, Check, Code2, Copy, Download, ExternalLink, File, Folder, Loader2, X } from 'lucide-react'
import { api, timeLabel } from '../api'
import { FileRef, rawUrl } from '../preview'
import { Markdown } from './Markdown'

interface FileData {
  path: string; galaxy: string; galaxy_name: string; found: string[]
  kind: 'markdown' | 'text' | 'image' | 'video' | 'audio' | 'pdf' | 'binary' | 'dir'
  size: number; mtime: number; text?: string | null; truncated?: boolean; error?: string
  entries?: { name: string; is_dir: boolean; size: number | null }[]
}

const LANG: Record<string, string> = { py: 'python', ts: 'typescript', tsx: 'tsx', js: 'javascript', json: 'json', sh: 'bash',
  yaml: 'yaml', yml: 'yaml', toml: 'toml', csv: 'csv', log: 'plaintext', txt: 'plaintext', html: 'html', css: 'css', sql: 'sql' }

function size(n?: number | null) {
  if (n == null) return ''
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`
}

/** Right-hand panel previewing a file that lives on some galaxy (ChatGPT-canvas style). */
export function FilePreview({ file, onClose, onOpen }: { file: FileRef; onClose: () => void; onOpen: (f: FileRef) => void }) {
  const [data, setData] = useState<FileData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [source, setSource] = useState(false)
  const [copied, setCopied] = useState(false)
  const [history, setHistory] = useState<FileRef[]>([])

  useEffect(() => {
    setData(null); setError(null); setSource(false)
    const q = new URLSearchParams({ path: file.path })
    if (file.galaxy) q.set('galaxy', file.galaxy)
    api<FileData>(`file?${q}`).then(setData).catch((e) => setError(e.message))
  }, [file.path, file.galaxy])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const go = (f: FileRef) => { setHistory((h) => [...h, file]); onOpen(f) }
  const back = () => { const prev = history[history.length - 1]; setHistory((h) => h.slice(0, -1)); if (prev) onOpen(prev) }
  const name = file.path.split('/').pop() || file.path
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
  const g = data?.galaxy ?? file.galaxy
  const isText = data && (data.kind === 'markdown' || data.kind === 'text') && data.text != null

  return (
    <aside className="flex h-full w-full flex-col border-l border-white/[0.07] bg-abyss-900/95 backdrop-blur-xl animate-rise">
      <header className="flex items-center gap-2 border-b border-white/[0.06] px-3 py-2.5">
        {history.length > 0 && <button className="btn-icon h-8 w-8" onClick={back} title="返回"><ArrowLeft className="h-4 w-4" /></button>}
        <div className="min-w-0 flex-1 pl-1">
          <p className="truncate text-sm font-medium text-white" title={file.path}>{name}</p>
          <p className="truncate text-[11px] text-slate-500">
            {data ? <>{data.galaxy_name} · {size(data.size)} · {timeLabel(data.mtime)}</> : file.path}
          </p>
        </div>
        {isText && data!.kind === 'markdown' && (
          <button className={`btn-icon h-8 w-8 ${source ? 'text-white bg-white/10' : ''}`} onClick={() => setSource(!source)} title="源码"><Code2 className="h-4 w-4" /></button>
        )}
        {isText && (
          <button className="btn-icon h-8 w-8" title="复制内容"
            onClick={() => { navigator.clipboard?.writeText(data!.text!); setCopied(true); setTimeout(() => setCopied(false), 1500) }}>
            {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
          </button>
        )}
        {data && data.kind !== 'dir' && (
          <>
            <a className="btn-icon h-8 w-8" href={rawUrl(data.path, g)} target="_blank" rel="noreferrer" title="新标签页打开"><ExternalLink className="h-4 w-4" /></a>
            <a className="btn-icon h-8 w-8" href={rawUrl(data.path, g, true)} title="下载"><Download className="h-4 w-4" /></a>
          </>
        )}
        <button className="btn-icon h-8 w-8" onClick={onClose} title="关闭"><X className="h-4 w-4" /></button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error && <p className="m-5 rounded-xl border border-rose-400/20 bg-rose-400/[0.06] p-4 text-sm text-rose-200">无法读取：{error}</p>}
        {!data && !error && <div className="grid h-40 place-items-center"><Loader2 className="h-5 w-5 animate-spin text-slate-500" /></div>}
        {data?.error && <p className="m-5 rounded-xl border border-amber-300/20 bg-amber-300/[0.05] p-4 text-sm text-amber-100">{data.error}</p>}
        {data && data.found.length > 1 && (
          <p className="mx-5 mt-4 text-[11px] text-slate-500">这个路径在多个星系上都存在：{data.found.join('、')}，当前显示 {data.galaxy}。
            {data.found.filter((x) => x !== data.galaxy).map((x) => (
              <button key={x} className="ml-1 text-cyan-300 hover:underline" onClick={() => go({ path: data.path, galaxy: x })}>看 {x}</button>
            ))}
          </p>
        )}
        {data?.truncated && <p className="mx-5 mt-4 text-[11px] text-amber-300/80">文件较大，只显示前 2 MB。完整内容请下载。</p>}
        {data && (
          <div className="px-5 py-4">
            {data.kind === 'markdown' && data.text != null && (source
              ? <pre className="whitespace-pre-wrap break-words font-mono text-[13px] text-slate-300">{data.text}</pre>
              : <Markdown text={data.text} base={data.path} galaxy={g} />)}
            {data.kind === 'text' && data.text != null && (
              ['csv', 'log', 'txt', ''].includes(ext) || data.size > 300_000
                ? <pre className="whitespace-pre-wrap break-words font-mono text-[12.5px] leading-relaxed text-slate-300">{data.text}</pre>
                : <Markdown text={'```' + (LANG[ext] ?? ext) + '\n' + data.text + '\n```'} />)}
            {data.kind === 'image' && <img src={rawUrl(data.path, g)} alt={name} className="mx-auto max-w-full rounded-xl" />}
            {data.kind === 'video' && <video src={rawUrl(data.path, g)} controls className="w-full rounded-xl bg-black" />}
            {data.kind === 'audio' && <audio src={rawUrl(data.path, g)} controls className="w-full" />}
            {data.kind === 'pdf' && <iframe src={rawUrl(data.path, g)} className="h-[80vh] w-full rounded-xl bg-white" title={name} />}
            {data.kind === 'binary' && <p className="text-sm text-slate-400">二进制文件，无法预览，可以下载查看。</p>}
            {data.kind === 'dir' && (
              <ul className="divide-y divide-white/[0.04] text-sm">
                {(data.entries ?? []).map((e) => (
                  <li key={e.name}>
                    <button className="flex w-full items-center gap-2 py-2 text-left text-slate-300 hover:text-white"
                      onClick={() => go({ path: `${data.path.replace(/\/$/, '')}/${e.name}`, galaxy: data.galaxy })}>
                      {e.is_dir ? <Folder className="h-4 w-4 text-amber-300" /> : <File className="h-4 w-4 text-slate-500" />}
                      <span className="flex-1 truncate">{e.name}</span>
                      {!e.is_dir && <span className="text-[11px] text-slate-600">{size(e.size)}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </aside>
  )
}
