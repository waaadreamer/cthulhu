import { memo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { Check, Copy } from 'lucide-react'

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
      <button
        onClick={copy}
        className="absolute right-2 top-2 z-10 rounded-lg bg-white/10 p-1.5 text-slate-300 opacity-0 transition hover:bg-white/20 group-hover:opacity-100"
        aria-label="复制代码"
      >
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
      <pre {...props}>{children}</pre>
    </div>
  )
}

export const Markdown = memo(function Markdown({ text, streaming }: { text: string; streaming?: boolean }) {
  return (
    <div
      className={`prose prose-invert prose-sm sm:prose-base max-w-none prose-p:leading-relaxed prose-headings:text-white prose-strong:text-white prose-li:my-0.5 ${
        streaming ? 'caret' : ''
      }`}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]} components={{ pre: CodeBlock }}>
        {text}
      </ReactMarkdown>
    </div>
  )
})
