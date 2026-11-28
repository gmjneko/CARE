import { Sparkles } from 'lucide-react'
import type { PageContribution } from '@renderer/shell/contracts'
import { SkillsPage } from './SkillsPage'

export const skillsPageContribution: PageContribution = {
  id: 'skills',
  title: 'Skill',
  icon: Sparkles,
  placement: 'sidebar-primary',
  order: 20,
  Component: SkillsPage
}
