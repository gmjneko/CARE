import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { Check } from 'lucide-react'
import { cn } from './cn'

export function MenuContent({
  className,
  sideOffset = 8,
  ...props
}: ComponentPropsWithoutRef<typeof DropdownMenu.Content>): React.JSX.Element {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        sideOffset={sideOffset}
        className={cn(
          'care-menu z-50 min-w-[180px] space-y-0.5 rounded-[14px] border border-[var(--border-card)] bg-[var(--bg-surface)] p-1.5 shadow-[var(--shadow-menu)] select-none',
          className
        )}
        {...props}
      />
    </DropdownMenu.Portal>
  )
}

interface MenuItemProps {
  icon?: ReactNode
  label: ReactNode
  description?: ReactNode
  selected?: boolean
  muted?: boolean
  onSelect?: () => void
  className?: string
}

export function MenuItem({
  icon,
  label,
  description,
  selected = false,
  muted = false,
  onSelect,
  className
}: MenuItemProps): React.JSX.Element {
  return (
    <DropdownMenu.Item
      onSelect={onSelect}
      className={cn(
        'flex cursor-pointer items-center justify-between gap-4 rounded-[9px] px-2.5 py-2 text-ui outline-none transition-colors duration-100 data-[highlighted]:bg-[var(--bg-hover)]',
        muted
          ? 'text-[var(--text-primary)]/75 data-[highlighted]:text-[var(--text-primary)]'
          : 'text-[var(--text-primary)]',
        className
      )}
    >
      <span className="flex min-w-0 items-center gap-2.5">
        {icon && (
          <span className="flex h-4 w-4 shrink-0 items-center justify-center text-[var(--text-primary)]/75">
            {icon}
          </span>
        )}
        <span className="min-w-0">
          <span className="block truncate font-medium leading-snug">{label}</span>
          {description && (
            <span className="mt-0.5 block truncate text-caption text-[var(--text-secondary)]">
              {description}
            </span>
          )}
        </span>
      </span>

      {selected && (
        <Check className="h-4 w-4 shrink-0 text-[var(--text-primary)]" strokeWidth={2} />
      )}
    </DropdownMenu.Item>
  )
}

export function MenuSeparator({ className }: { className?: string }): React.JSX.Element {
  return (
    <DropdownMenu.Separator
      className={cn('mx-2 my-1 h-px bg-[var(--border-hairline)]', className)}
    />
  )
}
