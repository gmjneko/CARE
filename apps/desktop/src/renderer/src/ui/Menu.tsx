import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { Check, ChevronRight } from 'lucide-react'
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
          <span className="flex h-4 w-4 shrink-0 items-center justify-center text-[var(--text-primary)]">
            {icon}
          </span>
        )}
        <span className="min-w-0">
          <span className="block truncate font-normal leading-snug">{label}</span>
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

export function MenuLabel({
  children,
  className
}: {
  children: ReactNode
  className?: string
}): React.JSX.Element {
  return (
    <DropdownMenu.Label
      className={cn(
        'px-2.5 pb-1 pt-1.5 text-caption font-normal text-[var(--text-muted)]',
        className
      )}
    >
      {children}
    </DropdownMenu.Label>
  )
}

export const MenuSub = DropdownMenu.Sub

interface MenuSubTriggerProps {
  icon?: ReactNode
  label: ReactNode
  description?: ReactNode
  selected?: boolean
  className?: string
}

export function MenuSubTrigger({
  icon,
  label,
  description,
  selected = false,
  className
}: MenuSubTriggerProps): React.JSX.Element {
  return (
    <DropdownMenu.SubTrigger
      className={cn(
        'flex cursor-pointer items-center justify-between gap-4 rounded-[9px] px-2.5 py-2 text-ui text-[var(--text-primary)] outline-none transition-colors duration-100 data-[highlighted]:bg-[var(--bg-hover)] data-[state=open]:bg-[var(--bg-hover)]',
        className
      )}
    >
      <span className="flex min-w-0 items-center gap-2.5">
        {icon && (
          <span className="flex h-4 w-4 shrink-0 items-center justify-center text-[var(--text-primary)]">
            {icon}
          </span>
        )}
        <span className="min-w-0">
          <span className="block truncate font-normal leading-snug">{label}</span>
          {description && (
            <span className="mt-0.5 block truncate text-caption text-[var(--text-secondary)]">
              {description}
            </span>
          )}
        </span>
      </span>

      <span className="flex shrink-0 items-center gap-1.5">
        {selected && (
          <Check className="h-4 w-4 shrink-0 text-[var(--text-primary)]" strokeWidth={2} />
        )}
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-[var(--text-secondary)]" />
      </span>
    </DropdownMenu.SubTrigger>
  )
}

export function MenuSubContent({
  className,
  sideOffset = 6,
  alignOffset = -6,
  collisionPadding = 8,
  ...props
}: ComponentPropsWithoutRef<typeof DropdownMenu.SubContent>): React.JSX.Element {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.SubContent
        sideOffset={sideOffset}
        alignOffset={alignOffset}
        collisionPadding={collisionPadding}
        className={cn(
          'care-menu z-50 min-w-[148px] space-y-0.5 rounded-[14px] border border-[var(--border-card)] bg-[var(--bg-surface)] p-1.5 shadow-[var(--shadow-menu)] select-none',
          className
        )}
        {...props}
      />
    </DropdownMenu.Portal>
  )
}

export function MenuSeparator({ className }: { className?: string }): React.JSX.Element {
  return (
    <DropdownMenu.Separator
      className={cn('mx-2 my-1 h-px bg-[var(--border-hairline)]', className)}
    />
  )
}
