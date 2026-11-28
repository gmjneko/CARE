import { describe, expect, it } from 'vitest'
import { normalizeMathDelimiters } from './math-normalize'

describe('normalizeMathDelimiters', () => {
  it('promotes standalone single-line $$ ... $$ to multi-line display math', () => {
    const input = '独立公式：\n\n$$ \\sum_{i=1}^{n} i = \\frac{n(n+1)}{2} $$'
    expect(normalizeMathDelimiters(input)).toBe(
      '独立公式：\n\n$$\n\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}\n$$'
    )
  })

  it('converts \\(...\\) and \\[...\\] outside code spans while keeping code intact', () => {
    const input = [
      '行内 \\(E = mc^2\\)，代码块里的 `\\(a+b\\)` 和 `$E = mc^2$` 不变。',
      '\\[ e^{i\\pi} + 1 = 0 \\]',
      '```tex',
      '$$ \\int_0^1 x dx $$',
      '```'
    ].join('\n')

    expect(normalizeMathDelimiters(input)).toBe(
      [
        '行内 $E = mc^2$，代码块里的 `\\(a+b\\)` 和 `$E = mc^2$` 不变。',
        '$$',
        'e^{i\\pi} + 1 = 0',
        '$$',
        '```tex',
        '$$ \\int_0^1 x dx $$',
        '```'
      ].join('\n')
    )
  })
})
