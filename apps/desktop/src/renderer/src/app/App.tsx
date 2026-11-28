import { useEffect } from 'react'
import * as Tooltip from '@radix-ui/react-tooltip'
import { NotificationsDialog } from '@renderer/features/notifications/NotificationsDialog'
import { SettingsDialog } from '@renderer/features/settings/SettingsDialog'
import { WorkbenchShell } from '@renderer/shell/WorkbenchShell'
import { useRuntimeStore } from '@renderer/stores/runtime-store'
import { useSessionStore } from '@renderer/stores/session-store'
import { bootstrapWorkbench } from './bootstrap'

bootstrapWorkbench()

export function App(): React.JSX.Element {
  const initialize = useRuntimeStore((s) => s.initialize)
  const client = useRuntimeStore((s) => s.client)
  const refreshThreads = useSessionStore((s) => s.refreshThreads)

  useEffect(() => {
    void initialize()
  }, [initialize])

  useEffect(() => {
    if (client) {
      void refreshThreads()
    }
  }, [client, refreshThreads])

  return (
    <Tooltip.Provider delayDuration={300}>
      <WorkbenchShell
        overlays={
          <>
            <SettingsDialog />
            <NotificationsDialog />
          </>
        }
      />
    </Tooltip.Provider>
  )
}
