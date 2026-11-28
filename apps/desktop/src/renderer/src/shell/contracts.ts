import type { ComponentType, ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import type { ProjectedToolCall } from '@renderer/sdk/event-projection'

export interface WorkbenchContext {
  mode: 'local' | 'enterprise'
  activeThreadId: string | null
  activeWorkspaceDir: string | null
  activeWorkflowId: string | null
}

/**
 * Pluggable top-level workbench page registered into PageRegistry.
 * Mirrors backend SPI / WorkflowRegistry so Sidebar & Shell stay business-agnostic.
 */
export interface PageContribution {
  id: string
  title: string
  icon: LucideIcon
  placement: 'sidebar-primary' | 'sidebar-secondary'
  order: number
  /** Optional custom click handler (e.g. 'New Task' resets chat to empty welcome state). */
  onSelect?: () => void
  when?: (ctx: WorkbenchContext) => boolean
  badge?: () => number | null
  Component: ComponentType
}

/**
 * Detail view descriptor rendered when any right-drawer widget expands in place.
 */
export interface InspectorDetailDescriptor {
  id: string
  title: string
  subtitle?: string
  render: () => ReactNode
}

export interface InspectorWidgetProps {
  threadId: string | null
  workspaceDir: string | null
  workflowId: string | null
  openDetail: (detail: InspectorDetailDescriptor) => void
  closeDetail: () => void
}

/**
 * Pluggable collapsible card stacked inside the right floating InspectorDrawer.
 */
export interface InspectorWidgetContribution {
  id: string
  title: string
  order: number
  defaultOpen?: boolean
  when?: (ctx: WorkbenchContext) => boolean
  headerBadge?: (
    props: Pick<InspectorWidgetProps, 'threadId' | 'workspaceDir' | 'workflowId'>
  ) => string | number | null
  Component: ComponentType<InspectorWidgetProps>
}

/**
 * Optional specialized renderer for specific tool names inside the chat timeline.
 */
export interface ToolRendererContribution {
  id: string
  order: number
  matches: (toolCall: ProjectedToolCall) => boolean
  Component: ComponentType<{ toolCall: ProjectedToolCall }>
}
