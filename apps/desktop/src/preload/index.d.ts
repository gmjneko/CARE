import type { CareDesktopBridge } from '@shared/ipc-contracts'

declare global {
  interface Window {
    careDesktop: CareDesktopBridge
  }
}
