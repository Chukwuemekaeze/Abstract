// Modal that fetches and shows a past run's build_output on demand. Opened from
// a failed run's short SHA in the version history. The transcript is fetched
// only while the modal is open (useProjectRunDetail enabled flag).

import { Loader2 } from 'lucide-react'

import { useProjectRunDetail } from '@/api/projects'
import { BuildOutputPanel } from '@/components/projects/BuildOutputPanel'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

export function BuildOutputModal({
  projectId,
  runId,
  onOpenChange,
}: {
  projectId: string
  // The run whose output to show, or null when the modal is closed.
  runId: string | null
  onOpenChange: (open: boolean) => void
}) {
  const open = runId !== null
  const detail = useProjectRunDetail(projectId, runId, { enabled: open })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Build output</DialogTitle>
          <DialogDescription>
            {detail.data
              ? `Commit ${detail.data.git_commit_sha.slice(0, 7)}`
              : 'Loading the recorded build transcript.'}
          </DialogDescription>
        </DialogHeader>
        {detail.isPending && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading output...
          </div>
        )}
        {detail.isError && (
          <p className="text-sm text-destructive">
            Could not load the build output.
          </p>
        )}
        {detail.data &&
          (detail.data.build_output ? (
            <BuildOutputPanel output={detail.data.build_output} />
          ) : (
            <p className="text-sm text-muted-foreground">
              This run did not capture any build output.
            </p>
          ))}
      </DialogContent>
    </Dialog>
  )
}
