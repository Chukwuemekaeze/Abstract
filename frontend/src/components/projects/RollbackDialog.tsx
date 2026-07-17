// Rollback confirmation dialog. Driven by the rollback-dialog store; the target
// run id comes from a version history row. Spells out exactly what rollback
// does, including that it uses the CURRENT env vars (not the ones from when the
// target version first ran). On failure the dialog stays open and shows the
// build output the backend returned.

import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractHardeningError } from '@/api/client'
import { useRollbackProjectMutation, type ProjectRun } from '@/api/projects'
import { BuildOutputPanel } from '@/components/projects/BuildOutputPanel'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useRollbackDialogStore } from '@/store/rollback-dialog'

export function RollbackDialog({
  projectId,
  runs,
}: {
  projectId: string
  runs: ProjectRun[]
}) {
  const { open, targetRunId, close } = useRollbackDialogStore()
  const rollback = useRollbackProjectMutation(projectId)
  const [failureOutput, setFailureOutput] = useState<string | null>(null)

  const target = runs.find((run) => run.id === targetRunId) ?? null
  const shortSha = target ? target.git_commit_sha.slice(0, 7) : ''
  const pending = rollback.isPending

  const handleClose = (next: boolean) => {
    if (pending || next) return
    setFailureOutput(null)
    close()
  }

  const onConfirm = async () => {
    if (!targetRunId) return
    setFailureOutput(null)
    try {
      await rollback.mutateAsync(targetRunId)
      toast.success(`Rolled back to ${shortSha}.`)
      close()
    } catch (err) {
      const parsed = extractHardeningError(err, 'Rolling back failed')
      // Keep the dialog open so the user can read the build transcript.
      setFailureOutput(parsed.output)
      if (!parsed.output) {
        toast.error(parsed.message)
      }
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent showCloseButton={!pending} className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Roll back to {shortSha}?</DialogTitle>
          <DialogDescription>
            Review what happens before you continue.
          </DialogDescription>
        </DialogHeader>

        <ul className="list-disc space-y-2 pl-5 text-sm text-muted-foreground">
          <li>
            Abstract will check out commit {shortSha} on the server, rebuild your
            containers, and restart them.
          </li>
          <li>
            This uses your current environment variables, not the ones that were
            set when this version originally ran.
          </li>
          <li>Downtime lasts as long as the build.</li>
        </ul>

        {failureOutput && (
          <div className="space-y-2">
            <p className="text-sm text-destructive">
              The rollback build failed. The server is left at commit {shortSha};
              you can roll back again or start forward.
            </p>
            <BuildOutputPanel output={failureOutput} />
          </div>
        )}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => handleClose(false)}
            disabled={pending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={onConfirm}
            disabled={pending || !target}
          >
            {pending && <Loader2 className="mr-1.5 size-3.5 animate-spin" />}
            Roll back
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
