import { Plug } from 'lucide-react'
import type { PageContribution } from '@renderer/shell/contracts'
import { McpPage } from './McpPage'

export const mcpPageContribution: PageContribution = {
  id: 'mcp',
  title: 'MCP',
  icon: Plug,
  placement: 'sidebar-primary',
  order: 30,
  Component: McpPage
}
