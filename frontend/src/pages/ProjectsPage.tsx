// Projects page: a table of every project on the left and a detail panel for the
// selected project on the right. Rendered inside the AppShell layout.

import { useMemo, useState } from 'react'
import { Loader2 } from 'lucide-react'

import { extractErrorMessage } from '@/api/client'
import { useProjects } from '@/api/projects'
import { NewProjectDialog } from '@/components/NewProjectDialog'
import { ProjectDetailPanel } from '@/components/projects/ProjectDetailPanel'
import { ProjectsTable } from '@/components/projects/ProjectsTable'
import { Button } from '@/components/ui/button'
import { useNewProjectStore } from '@/store/newProjectStore'

export function ProjectsPage() {
  const openNewProject = useNewProjectStore((s) => s.open)
  const projects = useProjects()
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const list = useMemo(() => projects.data ?? [], [projects.data])

  // The selected project drives the slide-out detail drawer; null means collapsed.
  // If the selected project disappears (e.g. after a delete) the drawer collapses.
  const selected = useMemo(
    () => list.find((p) => p.id === selectedId) ?? null,
    [list, selectedId],
  )

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-start justify-between px-8 py-10 pb-6">
        <div>
          <h1 className="text-3xl">Projects</h1>
          <p className="mt-1 text-sm text-text-dim">
            GitHub repos cloned onto your servers.
          </p>
        </div>
        <Button onClick={() => openNewProject()}>New project</Button>
      </header>

      {projects.isLoading && (
        <div className="flex items-center gap-3 px-8 py-8 text-text-dim">
          <Loader2 className="size-5 animate-spin" />
          <span>Loading projects...</span>
        </div>
      )}

      {projects.isError && (
        <p className="px-8 text-sm text-destructive">
          {extractErrorMessage(projects.error, 'Could not load projects.')}
        </p>
      )}

      {projects.data && list.length === 0 && (
        <div className="mx-8 rounded-lg border border-dashed border-border py-16 text-center">
          <p className="font-display text-lg font-bold tracking-[-0.02em]">
            No projects yet
          </p>
          <p className="mb-4 text-sm text-text-dim">
            Clone a GitHub repo onto one of your servers to get started.
          </p>
          <Button onClick={() => openNewProject()}>
            Create your first project
          </Button>
        </div>
      )}

      {list.length > 0 && (
        <div className="min-h-0 flex-1 overflow-y-auto px-8">
          <ProjectsTable
            projects={list}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>
      )}

      <ProjectDetailPanel
        project={selected}
        onClose={() => setSelectedId(null)}
      />

      <NewProjectDialog />
    </div>
  )
}
