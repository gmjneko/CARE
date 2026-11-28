import { MessageSquarePlus } from 'lucide-react'
import type { PageContribution } from '@renderer/shell/contracts'
import { useSessionStore } from '@renderer/stores/session-store'
import { ChatPage } from './ChatPage'

export const chatPageContribution: PageContribution = {
  id: 'chat',
  title: '新建任务',
  icon: MessageSquarePlus,
  placement: 'sidebar-primary',
  order: 10,
  onSelect: () => {
    useSessionStore.getState().startNewTask()
  },
  Component: ChatPage
}
