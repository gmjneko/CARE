import { chatPageContribution } from '@renderer/features/chat'
import { defaultInspectorWidgets } from '@renderer/features/inspector-widgets'
import { mcpPageContribution } from '@renderer/features/mcp'
import { skillsPageContribution } from '@renderer/features/skills'
import { inspectorWidgetRegistry, pageRegistry } from '@renderer/shell/registries'

let bootstrapped = false

/**
 * Single frontend assembly root registering all feature pages and right-drawer widgets.
 * Adding a new sidebar page or right-drawer widget only requires registering it here.
 */
export function bootstrapWorkbench(): void {
  if (bootstrapped) return
  bootstrapped = true

  pageRegistry.register(chatPageContribution)
  pageRegistry.register(skillsPageContribution)
  pageRegistry.register(mcpPageContribution)

  for (const widget of defaultInspectorWidgets) {
    inspectorWidgetRegistry.register(widget)
  }
}
