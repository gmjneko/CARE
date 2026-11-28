import type {
  InspectorWidgetContribution,
  PageContribution,
  ToolRendererContribution,
  WorkbenchContext
} from './contracts'

/**
 * Minimal ordered contribution registry (in-process SPI for frontend feature decoupling).
 */
export class ContributionRegistry<T extends { id: string; order: number }> {
  private readonly items = new Map<string, T>()

  public register(contribution: T): void {
    if (this.items.has(contribution.id)) {
      throw new Error(`Duplicate contribution id '${contribution.id}'`)
    }
    this.items.set(contribution.id, contribution)
  }

  public get(id: string): T | undefined {
    return this.items.get(id)
  }

  public list(): T[] {
    return Array.from(this.items.values()).sort((a, b) => a.order - b.order)
  }

  public clear(): void {
    this.items.clear()
  }
}

export const pageRegistry = new ContributionRegistry<PageContribution>()
export const inspectorWidgetRegistry = new ContributionRegistry<InspectorWidgetContribution>()
export const toolRendererRegistry = new ContributionRegistry<ToolRendererContribution>()

export function getVisiblePages(
  ctx: WorkbenchContext,
  placement?: PageContribution['placement']
): PageContribution[] {
  return pageRegistry.list().filter((page) => {
    if (placement && page.placement !== placement) return false
    return page.when ? page.when(ctx) : true
  })
}

export function getVisibleInspectorWidgets(ctx: WorkbenchContext): InspectorWidgetContribution[] {
  return inspectorWidgetRegistry.list().filter((widget) => (widget.when ? widget.when(ctx) : true))
}
