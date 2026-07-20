// Cancel a still-pending server registration. Asks for explicit confirmation, then
// calls POST /servers/{id}/cancel. When Abstract's key was already installed on the
// VPS (a partial install that then failed), the backend strips it off the box before
// deleting the row. If that remote cleanup cannot complete, the backend keeps the row
// and returns a structured 502; we render the reported per-step outcome so we never
// falsely claim the key was removed, and offer a retry (every step is idempotent).
//
// The dialog mounts only while open, so state starts fresh on every open. On success
// the row is gone, so we toast, close, and navigate to the server list.

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, Check, Loader2, MinusCircle, X } from 'lucide-react'
import { toast } from 'sonner'

import {
  extractServerDeletionError,
  useCancelServerMutation,
  useServer,
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
import { useCancelRegistrationDialogStore } from '@/store/cancel-registration-dialog'

// Human labels for the cancel step names the backend returns.
const STEP_LABELS: Record<string, string> = {
  connect_ssh: 'Connect to the server',
  restore_ssh_access: 'Re-enable password login',
  remove_authorized_key: "Remove Abstract's SSH key",
  evict_ssh_connection: 'Close the pooled SSH connection',
  delete_server_record: 'Delete the pending record',
}

interface Failure {
  message: string
  steps: ServerDeletionStepResult[]
}

export function CancelRegistrationDialog() {
  const open = useCancelRegistrationDialogStore((s) => s.open)
  const serverId = useCancelRegistrationDialogStore((s) => s.serverId)
  if (!open || !serverId) return null
  return <CancelDialogOpen serverId={serverId} />
}

function CancelDialogOpen({ serverId }: { serverId: string }) {
  const close = useCancelRegistrationDialogStore((s) => s.close)
  const navigate = useNavigate()
  const { data: server } = useServer(serverId)
  const cancel = useCancelServerMutation(serverId)

  const [failure, setFailure] = useState<Failure | null>(null)
  const pending = cancel.isPending

  const runCancel = async () => {
    setFailure(null)
    try {
      await cancel.mutateAsync()
      toast.success('Registration cancelled.')
      close()
      // The server list lives at the app root.
      navigate('/')
    } catch (err) {
      const parsed = extractServerDeletionError(err, 'Could not cancel the registration')
      setFailure({ message: parsed.message, steps: parsed.steps })
    }
  }

  return (
    <Dialog open onOpenChange={(v) => !pending && !v && close()}>
      <DialogContent showCloseButton={!pending} className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Cancel registration for {server?.name ?? ''}?</DialogTitle>
          <DialogDescription>
            This discards the pending registration. If Abstract already installed its
            key on the server, it is removed first. This cannot be undone.
          </DialogDescription>
        </DialogHeader>

        {failure ? (
          <FailureView failure={failure} />
        ) : pending ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Cancelling registration...
          </div>
        ) : (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
            <p className="mb-2 flex items-center gap-2 font-medium text-destructive">
              <AlertTriangle className="size-4" /> What happens:
            </p>
            <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
              <li>The pending server record is deleted.</li>
              <li>
                If Abstract's key was installed on the server, it is removed and
                password login is re-enabled so you can still log in.
              </li>
              <li>
                If the server can't be reached, nothing is deleted and you can retry
                once it is back online.
              </li>
            </ul>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => close()} disabled={pending}>
            Keep registration
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={runCancel}
            disabled={pending}
          >
            {pending && <Loader2 className="mr-2 size-4 animate-spin" />}
            {failure ? 'Retry cancellation' : 'Cancel registration'}
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
      {failure.steps.length > 0 && (
        <ol className="flex flex-col gap-2">
          {failure.steps.map((step, i) => (
            <StepRow key={`${step.name}-${i}`} step={step} />
          ))}
        </ol>
      )}
      <p className="text-xs text-muted-foreground">
        Nothing was deleted: the pending record is still here and each step is safe to
        re-run, so retrying picks up cleanly once the server is reachable again.
      </p>
    </div>
  )
}

function StepRow({ step }: { step: ServerDeletionStepResult }) {
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

function StepIcon({ status }: { status: ServerDeletionStepResult['status'] }) {
  if (status === 'completed') {
    return <Check className="mt-0.5 size-4 shrink-0 text-green-600" />
  }
  if (status === 'failed') {
    return <X className="mt-0.5 size-4 shrink-0 text-destructive" />
  }
  return <MinusCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
}
