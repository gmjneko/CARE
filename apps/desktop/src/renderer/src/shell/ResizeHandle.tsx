import { useRef, useState } from 'react'
import { cn } from '@renderer/ui/cn'

interface ResizeHandleProps {
  side: 'left' | 'right'
  currentWidth: number
  minWidth: number
  maxWidth: number
  defaultWidth: number
  onResize: (nextWidth: number) => void
  onResizingChange: (resizing: boolean) => void
}

/**
 * Sleek 8px hit-target vertical splitter handle with subtle hover/drag hairline.
 * Double-click resets width back to default.
 */
export function ResizeHandle({
  side,
  currentWidth,
  minWidth,
  maxWidth,
  defaultWidth,
  onResize,
  onResizingChange
}: ResizeHandleProps): React.JSX.Element {
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (e.button !== 0) return
    e.preventDefault()
    dragRef.current = { startX: e.clientX, startWidth: currentWidth }
    setDragging(true)
    onResizingChange(true)

    const prevCursor = document.body.style.cursor
    const prevUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const onMove = (moveEvent: PointerEvent): void => {
      const state = dragRef.current
      if (!state) return
      const delta =
        side === 'left' ? moveEvent.clientX - state.startX : state.startX - moveEvent.clientX
      const clamped = Math.max(minWidth, Math.min(maxWidth, Math.round(state.startWidth + delta)))
      onResize(clamped)
    }

    const onUp = (): void => {
      dragRef.current = null
      setDragging(false)
      onResizingChange(false)
      document.body.style.cursor = prevCursor
      document.body.style.userSelect = prevUserSelect
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={side === 'left' ? '调整左侧边栏宽度' : '调整右侧边栏宽度'}
      title="拖拽调整宽度，双击恢复默认"
      onPointerDown={handlePointerDown}
      onDoubleClick={() => onResize(defaultWidth)}
      className={cn(
        'group absolute bottom-0 top-0 z-30 flex w-2 cursor-col-resize items-stretch justify-center',
        side === 'left' ? '-right-1' : '-left-1'
      )}
    >
      <span
        className={cn(
          'w-px transition-colors duration-150',
          dragging
            ? 'bg-[var(--accent-primary)]/40'
            : 'bg-transparent group-hover:bg-[var(--text-muted)]/45'
        )}
      />
    </div>
  )
}
