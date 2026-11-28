import { useSessionStore } from '@renderer/stores/session-store'
import { ConversationView } from './ConversationView'
import { EmptyWelcomeView } from './EmptyWelcomeView'

export function ChatPage(): React.JSX.Element {
  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  return activeThreadId ? <ConversationView /> : <EmptyWelcomeView />
}
