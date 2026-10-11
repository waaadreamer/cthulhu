import { memo, useEffect, useId, useState } from 'react'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeHighlight from 'rehype-highlight'
import { findAndReplace } from 'mdast-util-find-and-replace'
import { Check, Copy, FileText } from 'lucide-react'
import 'katex/dist/katex.min.css'
import { FILE_PATH, isImage, isVideo, looksLikePath, rawUrl, resolvePath, usePreview } from '../preview'

const FILE_HREF = '#cthulhu-file='

/** Bare absolute file paths in text become links that open the preview panel. */
function remarkFileLinks() {
  return (tree: any) => findAndReplace(tree, [[FILE_PATH, (path: string) => ({
    type: 'link', url: FILE_HREF + encodeURIComponent(path), children: [{ type: 'text', value: path }],
  })]], { ignore: ['link', 'linkReference', 'code', 'inlineCode', 'math', 'inlineMath'] })
}

/** Models often write \( \) and \[ \]; remark-math wants dollars. Code is left alone. */
function normalizeMath(text: string) {
  return text.split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g).map((part, i) => i % 2 ? part : part
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => `\n$$\n${m.trim()}\n$$\n`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => `$${m.trim()}$`)).join('')
}

function CodeBlock({ children, ...props }: React.HTMLAttributes<HTMLPreElement>) {
  const [copied, setCopied] = useState(false)
  const copy = (e: React.MouseEvent) => {
    const text = (e.currentTarget.parentElement?.querySelector('code') as HTMLElement | null)?.innerText ?? ''
    navigator.clipboard?.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="group relative">
      <button onClick={copy} aria-label="复制代码"
        className="absolute right-2 top-2 z-10 rounded-lg bg-white/10 p-1.5 text-slate-300 opacity-0 transition hover:bg-white/20 group-hover:opacity-100">
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
      <pre {...props}>{children}</pre>
    </div>
  )
}

let mermaidReady: Promise<typeof import('mermaid').default> | null = null
function loadMermaid() {
  mermaidReady ??= import('mermaid').then(({ default: m }) => {
    m.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict', fontFamily: 'inherit' })
    return m
  })
  return mermaidReady
}

function Mermaid({ code }: { code: string }) {
  const id = 'mmd' + useId().replace(/:/g, '')
  const [svg, setSvg] = useState<string | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    let live = true
    loadMermaid().then((m) => m.render(id, code)).then((r) => live && setSvg(r.svg)).catch(() => live && setError(true))
    return () => { live = false }
  }, [code, id])
  if (error) return <pre><code>{code}</code></pre>
  if (!svg) return <div className="my-4 h-24 animate-pulse rounded-xl bg-white/[0.03]" />
  return <div className="my-4 flex justify-center overflow-x-auto rounded-xl border border-white/[0.06] bg-black/20 p-4" dangerouslySetInnerHTML={{ __html: svg }} />
}

function Video({ src }: { src: string }) {
  return <video src={src} controls preload="metadata" className="my-3 max-h-[480px] w-full rounded-xl border border-white/[0.06] bg-black" />
}

function embedUrl(href: string) {
  const yt = href.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]{11})/)
  if (yt) return `https://www.youtube-nocookie.com/embed/${yt[1]}`
  const bv = href.match(/bilibili\.com\/video\/(BV\w+)/)
  if (bv) return `https://player.bilibili.com/player.html?bvid=${bv[1]}&autoplay=0`
  return null
}

function FileLink({ path, children }: { path: string; children: React.ReactNode }) {
  const open = usePreview()
  return (
    <button type="button" onClick={() => open({ path })} title={`预览 ${path}`}
      className="inline break-all rounded-md bg-cyan-400/[0.08] px-1 text-left font-mono text-[0.9em] text-cyan-300 hover:bg-cyan-400/15">
      <FileText className="mr-0.5 inline h-3.5 w-3.5 -translate-y-px" />{children}
    </button>
  )
}

export const Markdown = memo(function Markdown({ text, streaming, base, galaxy }: {
  text: string; streaming?: boolean; base?: string; galaxy?: string
}) {
  const local = (src: string) => /^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith('//') || src.startsWith('#')
    ? src : rawUrl(resolvePath(src, base), galaxy)
  return (
    <div className={`prose prose-invert prose-sm sm:prose-base max-w-none prose-p:leading-relaxed prose-headings:text-white prose-strong:text-white prose-li:my-0.5 ${streaming ? 'caret' : ''}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkFileLinks]}
        rehypePlugins={[rehypeKatex, [rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        urlTransform={(url) => url.startsWith(FILE_HREF) ? url : defaultUrlTransform(url)}
        components={{
          pre: ({ children, ...props }) => {
            const child: any = Array.isArray(children) ? children[0] : children
            const cls: string = child?.props?.className ?? ''
            if (cls.includes('language-mermaid') && !streaming) {
              return <Mermaid code={String(child.props.children ?? '').replace(/\n$/, '')} />
            }
            return <CodeBlock {...props}>{children}</CodeBlock>
          },
          code: ({ className, children, ...props }) => {
            const s = String(children ?? '')
            if (!className && !s.includes('\n') && looksLikePath(s)) return <FileLink path={s}>{s}</FileLink>
            return <code className={className} {...props}>{children}</code>
          },
          a: ({ href = '', children }) => {
            if (href.startsWith(FILE_HREF)) return <FileLink path={decodeURIComponent(href.slice(FILE_HREF.length))}>{children}</FileLink>
            const bare = typeof children === 'string' && children === href
            if (bare && isImage(href)) return <a href={href} target="_blank" rel="noreferrer"><img src={href} alt="" loading="lazy" /></a>
            if (bare && isVideo(href)) return <Video src={href} />
            const embed = bare && embedUrl(href)
            if (embed) return <iframe src={embed} className="my-3 aspect-video w-full rounded-xl border border-white/[0.06]" allowFullScreen loading="lazy" />
            if (!/^https?:/i.test(href) && looksLikePath(href)) return <FileLink path={resolvePath(href, base)}>{children}</FileLink>
            return <a href={href} target="_blank" rel="noreferrer">{children}</a>
          },
          img: ({ src = '', alt }) => isVideo(src)
            ? <Video src={local(src)} />
            : <img src={local(src)} alt={alt ?? ''} loading="lazy" className="rounded-xl" />,
        }}>
        {normalizeMath(text)}
      </ReactMarkdown>
    </div>
  )
})
