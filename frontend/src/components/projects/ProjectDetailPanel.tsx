// The right-hand detail panel for the selected project. It is a thin composition
// layer over existing building blocks: RuntimeControls (Start/Restart, Pull),
// the PublishDialog, EnvFilesSection, and VersionHistorySection. The status line
// summarises runtime_status / active_operation in plain language.

import { useState } from 'react'
import { Loader2, Rocket } from 'lucide-react'

import { type Project } from '@/api/projects'
import { EnvFilesSection } from '@/components/projects/EnvFilesSection'
import { PublishDialog } from '@/components/projects/PublishDialog'
import { RuntimeControls } from '@/components/projects/RuntimeControls'
import { VersionHistorySection } from '@/components/projects/VersionHistorySection'
import { Button } from '@/components/ui/button'
import { relativeTime } from '@/lib/relativeTime'

function StatusLine({ project }: { project: Project }) {
  const on = project.server_name ? ` on ${project.server_name}` : ''

  if (project.active_operation) {
    const label =
      project.active_operation === 'publishing'
        ? 'Publishing'
        : project.active_operation === 'rolling_back'
          ? 'Rolling back'
          : project.active_operation === 'deleting'
            ? 'Deleting'
            : 'Starting'
    return (
      <span className="flex items-center gap-1.5 text-cyan">
        <Loader2 className="size-3.5 animate-spin" />
        {label}
        {on}
      </span>
    )
  }
  if (project.runtime_status === 'running') {
    const since = project.started_at
      ? ` since ${relativeTime(project.started_at)}`
      : ''
    return (
      <span className="text-text-dim">
        Running{since}
        {on}
      </span>
    )
  }
  if (project.runtime_status === 'failed') {
    return <span className="text-destructive">Failed to start{on}</span>
  }
  return <span className="text-text-dim">Not started{on}</span>
}

export function ProjectDetailPanel({ project }: { project: Project }) {
  const [publishOpen, setPublishOpen] = useState(false)

  return (
    <div className="flex h-full flex-col gap-6 overflow-y-auto border-l border-border p-6">
      <div>
        <h2 className="text-xl">{project.name}</h2>
        <p className="mt-1 text-sm">
          <StatusLine project={project} />
        </p>
        <p className="mt-1 font-mono text-xs text-text-dim">
          {project.github_repo_full_name}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <RuntimeControls project={project} />
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => setPublishOpen(true)}
          disabled={project.runtime_status !== 'running'}
          title={
            project.runtime_status === 'running'
              ? 'Point a domain at this app over HTTPS'
              : 'Start the project before publishing'
          }
        >
          <Rocket className="mr-1.5 size-3.5" />
          Publish
        </Button>
      </div>

      {project.domain && (
        <a
          href={`https://${project.domain}`}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-cyan hover:text-cyan-bright hover:underline"
        >
          {project.domain}
        </a>
      )}

      <EnvFilesSection projectId={project.id} />

      <VersionHistorySection project={project} />

      <PublishDialog
        project={project}
        serverHost={project.server_host}
        open={publishOpen}
        onOpenChange={setPublishOpen}
      />
    </div>
  )
}
