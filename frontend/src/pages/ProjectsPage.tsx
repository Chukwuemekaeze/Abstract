// Top-level projects page: every project across all servers, newest first,
// plus the New Project dialog (opened with a freely selectable server).

import { Loader2 } from 'lucide-react'

import { extractErrorMessage } from '@/api/client'
import { useProjects } from '@/api/projects'
import { Header } from '@/components/Header'
import { NewProjectDialog } from '@/components/NewProjectDialog'
import { ProjectCard } from '@/components/projects/ProjectCard'
import { Button } from '@/components/ui/button'
import { useNewProjectStore } from '@/store/newProjectStore'

export function ProjectsPage() {
  const openNewProject = useNewProjectStore((s) => s.open)
  const projects = useProjects()

  return (
    <>
      <Header />
      <div className="mx-auto max-w-5xl px-6 py-10">
        <header className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">Projects</h1>
            <p className="text-muted-foreground text-sm">
              GitHub repos cloned onto your servers with dedicated deploy keys.
            </p>
          </div>
          <Button onClick={() => openNewProject()}>New Project</Button>
        </header>

        {projects.isLoading && (
          <div className="flex items-center gap-3 py-8">
            <Loader2 className="size-5 animate-spin" />
            <span>Loading projects...</span>
          </div>
        )}

        {projects.isError && (
          <p className="text-destructive text-sm">
            {extractErrorMessage(projects.error, 'Could not load projects.')}
          </p>
        )}

        {projects.data && projects.data.length === 0 && (
          <div className="rounded-lg border border-dashed py-16 text-center">
            <p className="text-lg font-medium">No projects yet</p>
            <p className="text-muted-foreground mb-4 text-sm">
              Clone a GitHub repo onto one of your servers to get started.
            </p>
            <Button onClick={() => openNewProject()}>
              Create your first project
            </Button>
          </div>
        )}

        {projects.data && projects.data.length > 0 && (
          <div className="flex flex-col gap-4">
            {projects.data.map((project) => (
              <ProjectCard key={project.id} project={project} />
            ))}
          </div>
        )}

        <NewProjectDialog />
      </div>
    </>
  )
}
