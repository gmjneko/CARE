import { memo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Code2, Copy } from 'lucide-react'

interface CodeBlockProps {
  language: string | null
  code: string
}

function CodeBlock({ language, code }: CodeBlockProps): React.JSX.Element {
  const [copied, setCopied] = useState(false)

  const handleCopy = (): void => {
    void navigator.clipboard.writeText(code).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    })
  }

  return (
    <div className="my-3 overflow-hidden rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/60">
      <div className="flex items-center justify-between border-b border-[var(--border-hairline)] px-3.5 py-2 text-xs text-[var(--text-secondary)]">
        <span className="inline-flex items-center gap-1.5 font-mono">
          <Code2 className="h-3.5 w-3.5 text-[var(--text-muted)]" />
          {language || 'text'}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
        >
          {copied ? (
            <Check className="h-3.5 w-3.5 text-[var(--status-success)]" />
          ) : (
            <Copy className="h-3.5 w-3.5" />
          )}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="overflow-x-auto px-3.5 py-3 font-mono text-[12.5px] leading-relaxed text-[var(--text-primary)]">
        <code>{code}</code>
      </pre>
    </div>
  )
}

export const MarkdownRenderer = memo(function MarkdownRenderer({
  content
}: {
  content: string
}): React.JSX.Element {
  return (
    <div className="care-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className, children, ...props }) {
            const match = /language-([\w-]+)/.exec(className || '')
            const text = String(children).replace(/\n$/, '')
            const isBlock = Boolean(match) || text.includes('\n')
            if (isBlock) {
              return <CodeBlock language={match ? match[1] : null} code={text} />
            }
            return (
              <code className={className} {...props}>
                {children}
              </code>
            )
          }
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
})
