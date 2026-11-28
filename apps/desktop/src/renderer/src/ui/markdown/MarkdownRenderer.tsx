import { isValidElement, memo, useMemo, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'
import { Check, Code2, Copy } from 'lucide-react'
import { normalizeMathDelimiters } from './math-normalize'

interface CodeBlockProps {
  language: string | null
  rawCode: string
  children: ReactNode
}

function CodeBlock({ language, rawCode, children }: CodeBlockProps): React.JSX.Element {
  const [copied, setCopied] = useState(false)

  const handleCopy = (): void => {
    void navigator.clipboard.writeText(rawCode).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    })
  }

  return (
    <div className="my-3 overflow-hidden rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-subtle)]/60">
      <div className="flex items-center justify-between border-b border-[var(--border-hairline)] px-3.5 py-2 text-caption text-[var(--text-secondary)]">
        <span className="inline-flex items-center gap-1.5 font-mono">
          <Code2 className="h-3.5 w-3.5 text-[var(--text-muted)]" />
          {language || 'text'}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-caption text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
        >
          {copied ? (
            <Check className="h-3.5 w-3.5 text-[var(--status-success)]" />
          ) : (
            <Copy className="h-3.5 w-3.5" />
          )}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="m-0 overflow-x-auto bg-transparent px-3.5 py-3 font-mono text-code text-[var(--text-primary)]">
        {children}
      </pre>
    </div>
  )
}

function extractText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return extractText(node.props.children)
  return ''
}

export const MarkdownRenderer = memo(function MarkdownRenderer({
  content
}: {
  content: string
}): React.JSX.Element {
  const normalized = useMemo(() => normalizeMathDelimiters(content), [content])

  return (
    <div className="care-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          [rehypeKatex, { strict: false }],
          [rehypeHighlight, { detect: false, ignoreMissing: true }]
        ]}
        components={{
          a({ href, children, ...props }) {
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                {children}
              </a>
            )
          },
          table({ children, ...props }) {
            return (
              <div className="my-3 overflow-hidden rounded-xl border border-[var(--border-hairline)]">
                <div className="overflow-x-auto">
                  <table {...props}>{children}</table>
                </div>
              </div>
            )
          },
          pre({ children }) {
            let language: string | null = null
            if (isValidElement<{ className?: string; children?: ReactNode }>(children)) {
              const match = /language-([\w-]+)/.exec(children.props.className || '')
              language = match ? match[1] : null
            }
            const rawCode = extractText(children).replace(/\n$/, '')
            return (
              <CodeBlock language={language} rawCode={rawCode}>
                {children}
              </CodeBlock>
            )
          }
        }}
      >
        {normalized}
      </ReactMarkdown>
    </div>
  )
})
