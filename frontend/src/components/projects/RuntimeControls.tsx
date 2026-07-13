// Runtime state and controls: status badge, Start/Restart, Pull latest, and a
// refresh button. On a failed start or pull the captured output is the primary
// content of an error dialog (not hidden behind a click). On a successful
// start the build transcript is available behind an opt-in inline collapsible.

import { useState } from 'react'
import {
  ChevronDown,
  Download,
  Loader2,
  Play,
  RefreshCw,
  RotateCw,
} from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage, extractHardeningError } from '@/api/client'
import {
  usePullProjectMutation,
  useRefreshStatusMutation,
  useStartProjectMutation,
  type Project,
} from '@/api/projects'
import { BuildOutputPanel } from '@/components/projects/BuildOutputPanel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { relativeTime } from '@/lib/relativeTime'

export function RuntimeStatusBadge({ project }: { project: Project }) {
  if (project.runtime_status === 'running') {
    return (
      <Badge className="bg-green-600 text-white">
        Running
        {project.started_at && (
          <span className="ml-1 font-normal opacity-80">
            since {relativeTime(project.started_at).replace(' ago', '')} ago
          </span>
        )}
      </Badge>
    )
  }
  if (project.runtime_status === 'failed') {
    return <Badge className="bg-red-600 text-white">Failed to start</Badge>
  }
  return <Badge variant="outline">Not started</Badge>
}

export function RuntimeControls({ project }: { project: Project }) {
  const start = useStartProjectMutation(project.id)
  const pull = usePullProjectMutation(project.id)
  const refresh = useRefreshStatusMutation(project.id)

  const [failure, setFailure] = useState<{
    message: string
    output: string | null
    description: string
  } | null>(null)
  const [successOutput, setSuccessOutput] = useState<string | null>(null)
  const [showSuccessOutput, setShowSuccessOutput] = useState(false)

  const isRunning = project.runtime_status === 'running'

  const onStart = async () => {
    try {
      const result = await start.mutateAsync()
      setSuccessOutput(result.build_output ?? null)
      setShowSuccessOutput(false)
      toast.success('Project started.')
    } catch (err) {
      const parsed = extractHardeningError(err, 'Starting the project failed')
      setFailure({
        ...parsed,
        description:
          'The app was not started. Fix the issue and try again; retries are safe.',
      })
    }
  }

  const onPull = async () => {
    try {
      const result = await pull.mutateAsync()
      if (result.already_up_to_date) {
        toast.success(`Already up to date (${result.after_commit}).`)
      } else {
        const hint = isRunning ? ' Restart to apply the new code.' : ''
        toast.success(
          `Pulled ${result.before_commit} -> ${result.after_commit}.${hint}`,
        )
      }
    } catch (err) {
      const parsed = extractHardeningError(err, 'Pulling the latest code failed')
      setFailure({
        ...parsed,
        description:
          'The code on the server was not changed. Fix the issue and try again; retries are safe.',
      })
    }
  }

  const onRefresh = async () => {
    try {
      await refresh.mutateAsync()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Refreshing the status failed'))
    }
  }

  const busy = start.isPending || pull.isPending || refresh.isPending

  return (
    <>
      <Button
        type="button"
        size="sm"
        variant={project.runtime_status === 'failed' ? 'destructive' : 'default'}
        onClick={onStart}
        disabled={busy}
      >
        {start.isPending ? (
          <Loader2 className="mr-1.5 size-3.5 animate-spin" />
        ) : isRunning ? (
          <RotateCw className="mr-1.5 size-3.5" />
        ) : (
          <Play className="mr-1.5 size-3.5" />
        )}
        {isRunning ? 'Restart' : 'Start'}
      </Button>

      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={onPull}
        disabled={busy || !project.cloned_at}
        title="Fetch the newest code from GitHub onto the server"
      >
        {pull.isPending ? (
          <Loader2 className="mr-1.5 size-3.5 animate-spin" />
        ) : (
          <Download className="mr-1.5 size-3.5" />
        )}
        Pull latest
      </Button>

      <Button
        type="button"
        size="sm"
        variant="ghost"
        onClick={onRefresh}
        disabled={busy}
        title="Refresh status"
      >
        <RefreshCw
          className={cn('size-3.5', refresh.isPending && 'animate-spin')}
        />
      </Button>

      {successOutput && (
        <div className="w-full">
          <button
            type="button"
            onClick={() => setShowSuccessOutput((v) => !v)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ChevronDown
              className={cn(
                'size-3 transition-transform',
                !showSuccessOutput && '-rotate-90',
              )}
            />
            {showSuccessOutput ? 'Hide build output' : 'View build output'}
          </button>
          {showSuccessOutput && (
            <div className="mt-2">
              <BuildOutputPanel output={successOutput} />
            </div>
          )}
        </div>
      )}

      <Dialog open={failure !== null} onOpenChange={(v) => !v && setFailure(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{failure?.message}</DialogTitle>
            <DialogDescription>{failure?.description}</DialogDescription>
          </DialogHeader>
          {failure?.output && <BuildOutputPanel output={failure.output} />}
        </DialogContent>
      </Dialog>
    </>
  )
}
