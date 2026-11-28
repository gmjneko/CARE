/**
 * Normalize common LLM LaTeX math delimiters into remark-math compatible syntax while leaving
 * fenced code blocks and inline code spans untouched:
 * - Standalone single-line `$$ expr $$` -> multi-line `$$\nexpr\n$$` (true display mode)
 * - `\[ expr \]` -> `$$\nexpr\n$$`
 * - `\( expr \)` -> `$expr$`
 */
export function normalizeMathDelimiters(markdown: string): string {
  const lines = markdown.split('\n')
  const out: string[] = []
  let activeFence: { char: '`' | '~'; len: number } | null = null

  for (const line of lines) {
    const trimmed = line.trimStart()
    const fenceMatch = /^(`{3,}|~{3,})/.exec(trimmed)

    if (activeFence) {
      out.push(line)
      if (
        fenceMatch &&
        fenceMatch[1][0] === activeFence.char &&
        fenceMatch[1].length >= activeFence.len &&
        trimmed.slice(fenceMatch[1].length).trim() === ''
      ) {
        activeFence = null
      }
      continue
    }

    if (fenceMatch) {
      activeFence = { char: fenceMatch[1][0] as '`' | '~', len: fenceMatch[1].length }
      out.push(line)
      continue
    }

    // Standalone single-line display math: `$$ ... $$` or `\[ ... \]`
    const singleLineBlock = /^\s*(?:\$\$(.+?)\$\$|\\\[(.+?)\\\])\s*$/.exec(line)
    if (singleLineBlock) {
      const expr = (singleLineBlock[1] ?? singleLineBlock[2] ?? '').trim()
      if (expr) {
        out.push('$$', expr, '$$')
        continue
      }
    }

    out.push(replaceOutsideInlineCode(line))
  }

  return out.join('\n')
}

function replaceOutsideInlineCode(line: string): string {
  let result = ''
  let i = 0
  let segmentStart = 0

  while (i < line.length) {
    if (line[i] === '`') {
      let ticks = 1
      while (i + ticks < line.length && line[i + ticks] === '`') ticks += 1
      const delimiter = '`'.repeat(ticks)
      const closeIdx = line.indexOf(delimiter, i + ticks)
      if (closeIdx !== -1) {
        result += replaceInlineMath(line.slice(segmentStart, i))
        result += line.slice(i, closeIdx + ticks)
        i = closeIdx + ticks
        segmentStart = i
        continue
      }
      i += ticks
      continue
    }
    i += 1
  }

  result += replaceInlineMath(line.slice(segmentStart))
  return result
}

function replaceInlineMath(segment: string): string {
  return segment
    .replace(/\\\[(.+?)\\\]/g, (_, expr: string) => `\n$$\n${expr.trim()}\n$$\n`)
    .replace(/\\\((.+?)\\\)/g, (_, expr: string) => `$${expr.trim()}$`)
}
