import { describe, expect, it } from 'vitest'
import { SseFrameParser, type SseFrame } from './sse-parser'

describe('SseFrameParser (failure-mode suite)', () => {
  it('parses frames split across arbitrary chunk and CRLF boundaries', () => {
    const parser = new SseFrameParser()
    const raw = 'id: 1\r\nevent: text.delta\r\ndata: {"seq":1,"text":"你好"}\r\n\r\n'
    const collected: SseFrame[] = []
    for (const ch of raw) {
      collected.push(...parser.feed(ch))
    }
    expect(collected).toEqual([
      {
        id: '1',
        event: 'text.delta',
        data: '{"seq":1,"text":"你好"}'
      }
    ])
  })

  it('ignores keepalive comments and empty frames', () => {
    const parser = new SseFrameParser()
    const frames = parser.feed(
      ': keepalive\n\n: another comment\n\nid: 2\nevent: workflow.started\ndata: {"ok":true}\n\n'
    )
    expect(frames).toHaveLength(1)
    expect(frames[0]).toEqual({
      id: '2',
      event: 'workflow.started',
      data: '{"ok":true}'
    })
  })

  it('concatenates multi-line data fields and flushes trailing frame at EOF', () => {
    const parser = new SseFrameParser()
    const f1 = parser.feed('id: 7\ndata: line1\ndata: line2\n\n')
    expect(f1).toEqual([{ id: '7', event: null, data: 'line1\nline2' }])

    const f2 = parser.feed('id: 8\nevent: workflow.completed\ndata: {"done":true}')
    expect(f2).toEqual([])
    expect(parser.flush()).toEqual([
      { id: '8', event: 'workflow.completed', data: '{"done":true}' }
    ])
  })
})
