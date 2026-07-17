// Version history: every recorded run of a project, newest first. The current
// running version is highlighted; superseded versions offer a rollback; failed
// runs expose their build output. Rollback and build-output viewing are handled
// by the RollbackDialog (store driven) and BuildOutputModal rendered here.

import { useState } from 'react'
import { Loader2 } from 'lucide-react'

import {
  useProjectRuns,
  type Project,
  type ProjectRun,
} from '@/api/projects'
import { BuildOutputModal } from '@/components/projects/BuildOutputModal'
import { RollbackDialog } from '@/components/projects/RollbackDialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { relativeTime } from '@/lib/relativeTime'
import { useRollbackDialogStore } from '@/store/rollback-dialog'

function formatDuration(startedAt: string, finishedAt: string): string {
  const seconds = Math.max(
    0,
    Math.round(
      (new Date(finishedAt).getTime() - new Date(startedAt).getTime()) / 1000,
    ),
  )
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`
}

function RunStatusBadge({ status }: { status: ProjectRun['status'] }) {
  if (status === 'running') {
    return <Badge className="bg-green-600 text-white">Current</Badge>
  }
  if (status === 'failed') {
    return <Badge className="bg-red-600 text-white">Failed</Badge>
  }
  return <Badge variant="outline">Superseded</Badge>
}

function RunRow({
  run,
  onViewOutput,
}: {
  run: ProjectRun
  onViewOutput: (runId: string) => void
}) {
  const openRollback = useRollbackDialogStore((s) => s.openWith)
  const shortSha = run.git_commit_sha.slice(0, 7) || '(unknown)'

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
      <RunStatusBadge status={run.status} />
      {run.status === 'failed' ? (
        <button
          type="button"
          onClick={() => onViewOutput(run.id)}
          className="font-mono text-xs underline-offset-2 hover:underline"
          title="View build output"
        >
          {shortSha}
        </button>
      ) : (
        <span className="font-mono text-xs">{shortSha}</span>
      )}
      {run.git_ref && (
        <span className="text-xs text-muted-foreground">{run.git_ref}</span>
      )}
      <span className="text-xs text-muted-foreground">
        {relativeTime(run.started_at)}
      </span>
      {run.finished_at && (
        <span className="text-xs text-muted-foreground">
          {formatDuration(run.started_at, run.finished_at)}
        </span>
      )}
      <div className="ml-auto">
        {run.status === 'superseded' && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => openRollback(run.id)}
          >
            Roll back
          </Button>
        )}
      </div>
    </div>
  )
}

export function VersionHistorySection({ project }: { project: Project }) {
  const runs = useProjectRuns(project.id)
  const [outputRunId, setOutputRunId] = useState<string | null>(null)

  return (
    <div className="flex flex-col gap-1">
      <h4 className="text-sm font-medium">Version history</h4>

      {runs.isPending && (
        <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading history...
        </div>
      )}

      {runs.isError && (
        <p className="py-2 text-sm text-destructive">
          Could not load version history.
        </p>
      )}

      {runs.data && runs.data.length === 0 && (
        <p className="py-2 text-sm text-muted-foreground">
          No runs yet. Start the project to create the first run.
        </p>
      )}

      {runs.data && runs.data.length > 0 && (
        <div className="divide-y">
          {runs.data.map((run) => (
            <RunRow key={run.id} run={run} onViewOutput={setOutputRunId} />
          ))}
        </div>
      )}

      <RollbackDialog projectId={project.id} runs={runs.data ?? []} />
      <BuildOutputModal
        projectId={project.id}
        runId={outputRunId}
        onOpenChange={(open) => !open && setOutputRunId(null)}
      />
    </div>
  )
}
