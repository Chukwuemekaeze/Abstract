// The projects table: one selectable row per project across all servers. Each
// row shows the project name, the server it runs on, and a derived status.
//
// The COMMIT column from the mockup is intentionally omitted: the list endpoint
// carries no commit sha (it lives on ProjectRun), and it is already shown in the
// detail panel's version history.

import { type Project } from '@/api/projects'
import { cn } from '@/lib/utils'

type State = 'running' | 'deploying' | 'failed' | 'stopped'

function deriveState(project: Project): State {
  if (project.active_operation != null) return 'deploying'
  if (project.runtime_status === 'running') return 'running'
  if (project.runtime_status === 'failed') return 'failed'
  return 'stopped'
}

const STATE_META: Record<State, { label: string; dot: string; text: string }> = {
  running: { label: 'running', dot: 'bg-green-500', text: 'text-green-400' },
  deploying: { label: 'deploying', dot: 'bg-amber-400', text: 'text-amber-400' },
  failed: { label: 'failed', dot: 'bg-red-500', text: 'text-red-400' },
  stopped: { label: 'stopped', dot: 'bg-text-dim', text: 'text-text-dim' },
}

function StateCell({ project }: { project: Project }) {
  const meta = STATE_META[deriveState(project)]
  return (
    <span className={cn('flex items-center gap-2 text-sm', meta.text)}>
      <span className={cn('size-1.5 rounded-full', meta.dot)} />
      {meta.label}
    </span>
  )
}

export function ProjectsTable({
  projects,
  selectedId,
  onSelect,
}: {
  projects: Project[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  return (
    <div>
      <div className="grid grid-cols-[2fr_1.6fr_0.8fr] gap-4 border-b border-border px-4 pb-2 text-xs font-semibold uppercase tracking-[0.06em] text-text-dim">
        <span>Project</span>
        <span>Server</span>
        <span>Status</span>
      </div>

      {projects.map((project) => {
        const isSelected = project.id === selectedId
        return (
          <button
            key={project.id}
            type="button"
            data-project-row
            onClick={() => onSelect(project.id)}
            className={cn(
              'grid w-full grid-cols-[2fr_1.6fr_0.8fr] items-center gap-4 border-b border-l-2 border-border px-4 py-4 text-left transition-colors',
              isSelected
                ? 'border-l-transparent bg-cyan/15'
                : 'border-l-transparent hover:bg-card/50',
            )}
          >
            <div className="min-w-0">
              <div className="truncate font-medium text-foreground">
                {project.name}
              </div>
            </div>
            <div className="min-w-0 text-sm">
              <div className="truncate text-text-dim">
                {project.server_name ?? 'unknown server'}
              </div>
            </div>
            <StateCell project={project} />
          </button>
        )
      })}
    </div>
  )
}
