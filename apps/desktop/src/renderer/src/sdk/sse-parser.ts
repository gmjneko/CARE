/**
 * Incremental WHATWG Server-Sent Events (SSE) frame parser.
 * Supports arbitrary chunk splits, CRLF/LF/CR endings, multi-line data fields,
 * and silently skips comment frames (e.g. `: keepalive`).
 */

export interface SseFrame {
  id: string | null
  event: string | null
  data: string
}

export class SseFrameParser {
  private buffer = ''
  private eventId: string | null = null
  private eventName: string | null = null
  private dataLines: string[] = []

  public feed(chunk: string): SseFrame[] {
    if (!chunk) return []
    this.buffer += chunk
    const frames: SseFrame[] = []

    let lineStart = 0
    for (let i = 0; i < this.buffer.length; i += 1) {
      const ch = this.buffer[i]
      if (ch === '\r') {
        if (i + 1 >= this.buffer.length) {
          // Wait for next chunk in case '\n' follows '\r'
          break
        }
        const line = this.buffer.slice(lineStart, i)
        const emitted = this.processLine(line)
        if (emitted) frames.push(emitted)
        if (this.buffer[i + 1] === '\n') i += 1
        lineStart = i + 1
      } else if (ch === '\n') {
        const line = this.buffer.slice(lineStart, i)
        const emitted = this.processLine(line)
        if (emitted) frames.push(emitted)
        lineStart = i + 1
      }
    }

    this.buffer = this.buffer.slice(lineStart)
    return frames
  }

  public flush(): SseFrame[] {
    const frames: SseFrame[] = []
    if (this.buffer.length > 0) {
      const trailing = this.buffer.endsWith('\r') ? this.buffer.slice(0, -1) : this.buffer
      this.buffer = ''
      const emitted = this.processLine(trailing)
      if (emitted) frames.push(emitted)
    }
    const finalFrame = this.dispatchFrame()
    if (finalFrame) frames.push(finalFrame)
    return frames
  }

  private processLine(line: string): SseFrame | null {
    if (line === '') {
      return this.dispatchFrame()
    }
    if (line.startsWith(':')) {
      return null
    }
    const colonIdx = line.indexOf(':')
    let field: string
    let value: string
    if (colonIdx === -1) {
      field = line
      value = ''
    } else {
      field = line.slice(0, colonIdx)
      value = line.slice(colonIdx + 1)
      if (value.startsWith(' ')) {
        value = value.slice(1)
      }
    }

    if (field === 'id') {
      if (!value.includes('\0')) {
        this.eventId = value
      }
    } else if (field === 'event') {
      this.eventName = value
    } else if (field === 'data') {
      this.dataLines.push(value)
    }
    return null
  }

  private dispatchFrame(): SseFrame | null {
    if (this.dataLines.length === 0) {
      this.eventName = null
      return null
    }
    const frame: SseFrame = {
      id: this.eventId,
      event: this.eventName,
      data: this.dataLines.join('\n')
    }
    this.eventName = null
    this.dataLines = []
    return frame
  }
}
