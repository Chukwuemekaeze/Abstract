// Delete a server. Shows the projects that will be destroyed (fetched fresh when
// the dialog opens), a fixed warning of what stays and what is undone on the VPS,
// and a type-to-confirm gate on the server name. While the delete runs it shows an
// optimistic ordered checklist (each project, then the VPS teardown steps); on
// failure it renders the real per-step result the backend returned, including which
// project failed, with a retry that re-runs from the top. Retries are safe: every
// backend step is idempotent.
//
// The dialog mounts only while open, so state starts fresh on every open. On
// success the server row is gone, so we toast, close, and navigate back to the
// server list. The backend returns the full step list only at the end (no
// streaming), so the live progress is optimistic and the returned steps matter on
// the failure path.

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, Check, Loader2, MinusCircle, X } from 'lucide-react'
import { toast } from 'sonner'

import {
  extractServerDeletionError,
  useDeleteServerMutation,
  useServer,
  useServerDeletionPreview,
  type ServerDeletionPreviewProject,
  type ServerDeletionStepResult,
} from '@/api/servers'
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
import { Badge } from '@/components/ui/badge'
import { useDeleteServerDialogStore } from '@/store/delete-server-dialog'

// The four VPS teardown steps after the per-project loop, in run order. Sudoers
// is revoked last: it is the step that removes passwordless sudo, so every earlier
// sudo command must run before it.
const VPS_STEPS: { name: string; label: string }[] = [
  { name: 'restore_ssh_access', label: 'Restoring SSH access' },
  { name: 'remove_authorized_key', label: "Removing Abstract's SSH key" },
  { name: 'revoke_sudoers', label: 'Revoking sudoers grant' },
  { name: 'delete_server_record', label: 'Deleting server record' },
]

// Human labels for every backend step name, for the returned failure view.
const STEP_LABELS: Record<string, string> = {
  delete_project: 'Delete project',
  connect_ssh: 'Connect to the server',
  revoke_sudoers: "Revoke Abstract's sudoers grant",
  restore_ssh_access: 'Restore password and root SSH login',
  remove_authorized_key: "Remove Abstract's SSH key",
  evict_ssh_connection: 'Close the pooled SSH connection',
  delete_server_record: 'Delete the server record',
}

// Same teardown order the backend loops in: running first, then failed, then
// never_started. The preview arrives created_at ascending, and Array.sort is
// stable, so a group-only sort preserves the created_at tie-break.
const RUNTIME_ORDER: Record<ServerDeletionPreviewProject['runtime_status'], number> = {
  running: 0,
  failed: 1,
  never_started: 2,
}

interface Failure {
  message: string
  failedStep: string | null
  failedProjectName: string | null
  steps: ServerDeletionStepResult[]
}

export function DeleteServerDialog() {
  const open = useDeleteServerDialogStore((s) => s.open)
  const serverId = useDeleteServerDialogStore((s) => s.serverId)
  if (!open || !serverId) return null
  return <DeleteDialogOpen serverId={serverId} />
}

function DeleteDialogOpen({ serverId }: { serverId: string }) {
  const close = useDeleteServerDialogStore((s) => s.close)
  const navigate = useNavigate()
  const { data: server } = useServer(serverId)
  const preview = useServerDeletionPreview(serverId, true)
  const del = useDeleteServerMutation(serverId)

  const [confirm, setConfirm] = useState('')
  const [failure, setFailure] = useState<Failure | null>(null)

  const pending = del.isPending
  const confirmed = server ? confirm === server.name : false

  const projects = preview.data
    ? [...preview.data.projects].sort(
        (a, b) => RUNTIME_ORDER[a.runtime_status] - RUNTIME_ORDER[b.runtime_status],
      )
    : []

  const runDelete = async () => {
    setFailure(null)
    try {
      await del.mutateAsync()
      toast.success('Server deleted.')
      close()
      // The server list lives at the app root.
      navigate('/')
    } catch (err) {
      setFailure(extractServerDeletionError(err))
    }
  }

  return (
    <Dialog open onOpenChange={(v) => !pending && !v && close()}>
      <DialogContent showCloseButton={!pending} className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Delete server {server?.name ?? ''}?</DialogTitle>
          <DialogDescription>
            This tears down every project on this server, then removes Abstract from
            the VPS. This cannot be undone.
          </DialogDescription>
        </DialogHeader>

        {failure ? (
          <FailureView failure={failure} />
        ) : pending ? (
          <ProgressView projects={projects} />
        ) : (
          <>
            <ProjectPreview projects={projects} loading={preview.isLoading} />

            <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
              <p className="mb-2 flex items-center gap-2 font-medium text-destructive">
                <AlertTriangle className="size-4" /> Before you delete:
              </p>
              <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                <li>
                  All {projects.length} projects on this server will be deleted,
                  including their containers, images, named volumes, clones, GitHub
                  deploy keys, nginx configs, and TLS certificates.
                </li>
                <li>Abstract's SSH key will be removed from the server.</li>
                <li>
                  Password login and root SSH login will be re-enabled so you can log
                  back in. You will need your root password from your VPS provider. If
                  you have lost it, most providers let you reset it from their control
                  panel.
                </li>
                <li>
                  Docker, nginx, the firewall, the swap file, and the sudo user
                  Abstract created will remain on the server. The sudo user's
                  password-less sudo privilege will be revoked, but the account itself
                  stays.
                </li>
              </ul>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="confirm-name">
                Type <span className="font-mono">{server?.name}</span> to confirm
              </Label>
              <Input
                id="confirm-name"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                disabled={pending}
                autoComplete="off"
                placeholder={server?.name}
              />
            </div>
          </>
        )}

        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => close()}
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
            {failure ? 'Retry deletion' : 'Delete server'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function ProjectPreview({
  projects,
  loading,
}: {
  projects: ServerDeletionPreviewProject[]
  loading: boolean
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        Loading projects...
      </div>
    )
  }
  if (projects.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No projects on this server.</p>
    )
  }
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-sm font-medium">
        Projects that will be deleted ({projects.length})
      </p>
      <ul className="flex flex-col gap-1.5">
        {projects.map((p) => (
          <li
            key={p.id}
            className="flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm"
          >
            <div className="flex min-w-0 flex-col">
              <span className="truncate font-medium">{p.name}</span>
              {p.domain && (
                <span className="truncate text-xs text-muted-foreground">
                  {p.domain}
                </span>
              )}
            </div>
            <RuntimeBadge status={p.runtime_status} />
          </li>
        ))}
      </ul>
    </div>
  )
}

// Optimistic progress while the delete runs. The backend returns the full step
// list only at the end, so this is an ordered checklist of the work in flight
// rather than a live cursor: each project in teardown order, then the VPS steps.
function ProgressView({ projects }: { projects: ServerDeletionPreviewProject[] }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        Deleting server. This may take a while.
      </div>
      <ol className="flex flex-col gap-2">
        {projects.map((p, i) => (
          <li key={p.id} className="flex items-start gap-2 text-sm">
            <MinusCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span>
              Deleting project {i + 1} of {projects.length}: {p.name}
            </span>
          </li>
        ))}
        {VPS_STEPS.map((step) => (
          <li key={step.name} className="flex items-start gap-2 text-sm">
            <MinusCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span>{step.label}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

function FailureView({ failure }: { failure: Failure }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-destructive">{failure.message}</p>
      {failure.failedProjectName && (
        <p className="text-sm text-muted-foreground">
          The failure was inside project{' '}
          <span className="font-medium">{failure.failedProjectName}</span>.
        </p>
      )}
      <ol className="flex flex-col gap-2">
        {failure.steps.map((step, i) => (
          <StepRow key={`${step.name}-${i}`} step={step} />
        ))}
      </ol>
      <p className="text-xs text-muted-foreground">
        Nothing was left half-deleted: completed steps stay done and each step is safe
        to re-run, so retrying picks up cleanly once you have fixed the underlying
        issue.
      </p>
    </div>
  )
}

function StepRow({ step }: { step: ServerDeletionStepResult }) {
  const base = STEP_LABELS[step.name] ?? step.name
  const label =
    step.name === 'delete_project' && step.project_name
      ? `Delete project ${step.project_name}`
      : base
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

function StepIcon({ status }: { status: ServerDeletionStepResult['status'] }) {
  if (status === 'completed') {
    return <Check className="mt-0.5 size-4 shrink-0 text-green-600" />
  }
  if (status === 'failed') {
    return <X className="mt-0.5 size-4 shrink-0 text-destructive" />
  }
  return <MinusCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
}

function RuntimeBadge({
  status,
}: {
  status: ServerDeletionPreviewProject['runtime_status']
}) {
  const meta: Record<
    ServerDeletionPreviewProject['runtime_status'],
    { label: string; className: string }
  > = {
    running: { label: 'running', className: 'bg-green-600 text-white' },
    failed: { label: 'failed', className: 'bg-red-600 text-white' },
    never_started: { label: 'never started', className: 'bg-muted text-foreground' },
  }
  const m = meta[status]
  return <Badge className={m.className}>{m.label}</Badge>
}
