// Delete a project. Type-to-confirm gate, an explicit list of what gets torn
// down, and (on failure) a per-step progress view showing exactly where the
// deletion stopped with a retry that re-runs from the top. Retries are safe:
// every step on the backend is idempotent.
//
// The dialog mounts only while open, so state starts fresh on every open. On
// success the project row is gone and its card unmounts, so we just toast and
// close; the per-step view is what matters on the failure path.

import { useState } from 'react'
import { AlertTriangle, Check, Loader2, MinusCircle, X } from 'lucide-react'
import { toast } from 'sonner'

import {
  extractDeletionError,
  useDeleteProjectMutation,
  type DeletionStepResult,
  type Project,
} from '@/api/projects'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

const DELETION_TARGETS = [
  'Containers, images, and named volumes will be removed (docker compose down -v --rmi all)',
  'The clone directory on the server',
  'The HTTPS certificate and nginx site config',
  'The GitHub deploy key',
  'All env files and their variables',
]

// Human labels for the backend step names, kept in the fixed run order.
const STEP_LABELS: Record<string, string> = {
  connect_ssh: 'Connect to the server',
  unpublish: 'Remove domain, certificate, and nginx config',
  remove_docker_artifacts: 'Remove containers, images, and volumes',
  delete_clone: 'Delete the clone directory',
  remove_ssh_config_block: 'Remove the SSH config entry',
  delete_vps_deploy_key_files: 'Delete the deploy key files',
  revoke_github_deploy_key: 'Revoke the GitHub deploy key',
  delete_db_row: 'Delete the project record',
}

interface Failure {
  message: string
  failedStep: string | null
  steps: DeletionStepResult[]
}

export function DeleteProjectDialog({
  project,
  open,
  onOpenChange,
}: {
  project: Project
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  if (!open) return null
  return <DeleteDialogOpen project={project} onOpenChange={onOpenChange} />
}

function DeleteDialogOpen({
  project,
  onOpenChange,
}: {
  project: Project
  onOpenChange: (open: boolean) => void
}) {
  const del = useDeleteProjectMutation(project.id, project.server_id)
  const [confirm, setConfirm] = useState('')
  const [failure, setFailure] = useState<Failure | null>(null)

  const pending = del.isPending
  const confirmed = confirm.trim() === project.name

  const runDelete = async () => {
    setFailure(null)
    try {
      await del.mutateAsync()
      toast.success('Project deleted.')
      onOpenChange(false)
    } catch (err) {
      setFailure(extractDeletionError(err))
    }
  }

  return (
    <Dialog open onOpenChange={(v) => !pending && onOpenChange(v)}>
      <DialogContent showCloseButton={!pending} className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Delete {project.name}?</DialogTitle>
          <DialogDescription>
            This permanently removes the project and everything Abstract created
            for it. This cannot be undone.
          </DialogDescription>
        </DialogHeader>

        {failure ? (
          <FailureView failure={failure} />
        ) : (
          <>
            <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
              <p className="mb-2 flex items-center gap-2 font-medium text-destructive">
                <AlertTriangle className="size-4" /> This will delete:
              </p>
              <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                {DELETION_TARGETS.map((target) => (
                  <li key={target}>{target}</li>
                ))}
              </ul>
              <p className="mt-2 text-muted-foreground">
                This includes application data stored in docker named volumes,
                such as databases, uploads, and caches. This cannot be undone.
              </p>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="confirm-name">
                Type <span className="font-mono">{project.name}</span> to confirm
              </Label>
              <Input
                id="confirm-name"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                disabled={pending}
                autoComplete="off"
                placeholder={project.name}
              />
            </div>
          </>
        )}

        {pending && (
          <div className="flex items-center gap-3 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Deletion in progress. This may take a moment.
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={pending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={runDelete}
            disabled={pending || (!failure && !confirmed)}
          >
            {pending && <Loader2 className="mr-2 size-4 animate-spin" />}
            {failure ? 'Retry deletion' : 'Delete project'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function FailureView({ failure }: { failure: Failure }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-destructive">{failure.message}</p>
      <ol className="flex flex-col gap-2">
        {failure.steps.map((step) => (
          <StepRow key={step.name} step={step} />
        ))}
      </ol>
      <p className="text-xs text-muted-foreground">
        Nothing was left half-deleted: each step is safe to re-run, so retrying
        picks up cleanly.
      </p>
    </div>
  )
}

function StepRow({ step }: { step: DeletionStepResult }) {
  const label = STEP_LABELS[step.name] ?? step.name
  return (
    <li className="flex items-start gap-2 text-sm">
      <StepIcon status={step.status} />
      <div className="flex min-w-0 flex-col">
        <span className={step.status === 'failed' ? 'text-destructive' : undefined}>
          {label}
          {step.status === 'skipped' && (
            <span className="ml-1 text-xs text-muted-foreground">(skipped)</span>
          )}
        </span>
        {step.status === 'failed' && step.detail && (
          <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-muted p-2 text-xs">
            <code>{step.detail}</code>
          </pre>
        )}
      </div>
    </li>
  )
}

function StepIcon({ status }: { status: DeletionStepResult['status'] }) {
  if (status === 'completed') {
    return <Check className="mt-0.5 size-4 shrink-0 text-green-600" />
  }
  if (status === 'failed') {
    return <X className="mt-0.5 size-4 shrink-0 text-destructive" />
  }
  return <MinusCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
}
