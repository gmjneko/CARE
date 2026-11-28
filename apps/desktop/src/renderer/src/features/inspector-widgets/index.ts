import type { InspectorWidgetContribution } from '@renderer/shell/contracts'
import { ArtifactsWidget, EnvironmentWidget, ReferencesWidget, TaskListWidget } from './widgets'

export const defaultInspectorWidgets: InspectorWidgetContribution[] = [
  {
    id: 'environment',
    title: '环境信息',
    order: 10,
    defaultOpen: true,
    Component: EnvironmentWidget
  },
  {
    id: 'tasks',
    title: '任务清单',
    order: 20,
    defaultOpen: true,
    Component: TaskListWidget
  },
  {
    id: 'artifacts',
    title: '产物',
    order: 30,
    defaultOpen: true,
    Component: ArtifactsWidget
  },
  {
    id: 'references',
    title: '参考',
    order: 40,
    defaultOpen: true,
    Component: ReferencesWidget
  }
]
