// Publish dialog: domain + internal port -> nginx + Let's Encrypt on the VPS.
//
// Ports are detected from the running containers only while the dialog is
// open. Dangerous (database-looking) ports are hidden from the select; manual
// entry overrides. The publishing phase blocks closing; success shows the
// live URL and auto-closes.
//
// The dialog mounts only while open, so every open starts from a clean form
// with no reset effects; form values survive an error for retry because the
// component stays mounted for the whole session.

import { useEffect, useState } from 'react'
import { Check, ChevronDown, Copy, ExternalLink, Loader2 } from 'lucide-react'

import { extractHardeningError } from '@/api/client'
import {
  useDetectedPorts,
  usePublishProjectMutation,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'

type Phase = 'form' | 'publishing' | 'success'

export function PublishDialog({
  project,
  serverHost,
  open,
  onOpenChange,
}: {
  project: Project
  serverHost: string | undefined
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  if (!open) return null
  return (
    <PublishDialogOpen
      project={project}
      serverHost={serverHost}
      onOpenChange={onOpenChange}
    />
  )
}

function PublishDialogOpen({
  project,
  serverHost,
  onOpenChange,
}: {
  project: Project
  serverHost: string | undefined
  onOpenChange: (open: boolean) => void
}) {
  const detectedPorts = useDetectedPorts(project.id, { enabled: true })
  const publish = usePublishProjectMutation(project.id)

  const [phase, setPhase] = useState<Phase>('form')
  const [domain, setDomain] = useState('')
  const [selectedPort, setSelectedPort] = useState('')
  const [manualEntry, setManualEntry] = useState(false)
  const [manualPort, setManualPort] = useState('')
  const [error, setError] = useState<{
    message: string
    output: string | null
  } | null>(null)
  const [showOutput, setShowOutput] = useState(false)
  const [copied, setCopied] = useState(false)

  // Success view auto-closes after a short pause.
  useEffect(() => {
    if (phase !== 'success') return
    const timer = setTimeout(() => onOpenChange(false), 3000)
    return () => clearTimeout(timer)
  }, [phase, onOpenChange])

  const safePorts = (detectedPorts.data ?? []).filter((p) => !p.is_dangerous)
  const hiddenCount = (detectedPorts.data ?? []).length - safePorts.length

  const copyHost = async () => {
    if (!serverHost) return
    await navigator.clipboard.writeText(serverHost)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const effectivePort = manualEntry ? manualPort : selectedPort
  const portNumber = Number(effectivePort)
  const canSubmit =
    domain.trim() !== '' &&
    Number.isInteger(portNumber) &&
    portNumber >= 1 &&
    portNumber <= 65535

  const submit = async () => {
    setError(null)
    setPhase('publishing')
    try {
      await publish.mutateAsync({
        domain: domain.trim().toLowerCase(),
        internal_port: portNumber,
      })
      setPhase('success')
    } catch (err) {
      setError(extractHardeningError(err, 'Publishing failed'))
      setShowOutput(false)
      setPhase('form')
    }
  }

  const isPublishing = phase === 'publishing'

  return (
    <Dialog open onOpenChange={(v) => !isPublishing && onOpenChange(v)}>
      <DialogContent showCloseButton={!isPublishing}>
        {phase === 'success' ? (
          <>
            <DialogHeader>
              <DialogTitle>Published</DialogTitle>
              <DialogDescription>Your project is live at</DialogDescription>
            </DialogHeader>
            <a
              href={`https://${domain.trim().toLowerCase()}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-lg font-medium text-green-700 hover:underline"
            >
              https://{domain.trim().toLowerCase()}
              <ExternalLink className="size-4" />
            </a>
          </>
        ) : isPublishing ? (
          <>
            <DialogHeader>
              <DialogTitle>Publishing {project.name}</DialogTitle>
              <DialogDescription>
                Publishing your project. This may take up to a minute: nginx is
                configured and a Let's Encrypt certificate is issued.
              </DialogDescription>
            </DialogHeader>
            <div className="flex items-center justify-center py-8">
              <Loader2 className="size-6 animate-spin text-muted-foreground" />
            </div>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Publish {project.name}</DialogTitle>
              <DialogDescription>
                Point a domain at your running app over HTTPS.
              </DialogDescription>
            </DialogHeader>

            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="publish-domain">Domain</Label>
                <Input
                  id="publish-domain"
                  value={domain}
                  onChange={(e) => setDomain(e.target.value)}
                  placeholder="app.example.com"
                />
                {serverHost && (
                  <p className="flex items-center gap-1 text-xs text-muted-foreground">
                    Point your DNS A record for this domain at{' '}
                    <span className="font-mono">{serverHost}</span>
                    <button
                      type="button"
                      onClick={copyHost}
                      className="text-muted-foreground hover:text-foreground"
                      title="Copy IP address"
                    >
                      {copied ? (
                        <Check className="size-3" />
                      ) : (
                        <Copy className="size-3" />
                      )}
                    </button>{' '}
                    before continuing.
                  </p>
                )}
              </div>

              <div className="flex flex-col gap-1.5">
                <Label>Internal port</Label>
                {detectedPorts.isPending ? (
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Loader2 className="size-3.5 animate-spin" />
                    Detecting published ports...
                  </div>
                ) : manualEntry ? (
                  <Input
                    type="number"
                    min={1}
                    max={65535}
                    value={manualPort}
                    onChange={(e) => setManualPort(e.target.value)}
                    placeholder="8080"
                  />
                ) : safePorts.length > 0 ? (
                  <Select value={selectedPort} onValueChange={setSelectedPort}>
                    <SelectTrigger>
                      <SelectValue placeholder="Select a detected port" />
                    </SelectTrigger>
                    <SelectContent>
                      {safePorts.map((p) => (
                        <SelectItem
                          key={`${p.service}-${p.host_port}`}
                          value={String(p.host_port)}
                        >
                          {p.service} on port {p.host_port}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
                    No ports are published from your containers. Add a{' '}
                    <code>ports:</code> section to your docker-compose.yml,
                    pull the latest code, and restart your app.
                  </div>
                )}
                {!manualEntry && hiddenCount > 0 && (
                  <p className="text-xs text-amber-700">
                    {hiddenCount} port{hiddenCount === 1 ? ' was' : 's were'}{' '}
                    hidden because they look like database ports. Use manual
                    entry to override if you know what you are doing.
                  </p>
                )}
                <button
                  type="button"
                  onClick={() => setManualEntry((v) => !v)}
                  className="self-start text-xs text-muted-foreground underline hover:text-foreground"
                >
                  {manualEntry
                    ? 'Back to detected ports'
                    : 'Port not listed? Enter manually'}
                </button>
              </div>

              {error && (
                <div className="flex flex-col gap-1">
                  <p className="text-sm text-red-600">{error.message}</p>
                  {error.output && (
                    <div>
                      <button
                        type="button"
                        onClick={() => setShowOutput((v) => !v)}
                        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      >
                        <ChevronDown
                          className={cn(
                            'size-3 transition-transform',
                            !showOutput && '-rotate-90',
                          )}
                        />
                        {showOutput ? 'Hide output' : 'Show output'}
                      </button>
                      {showOutput && (
                        <pre className="mt-2 max-h-56 overflow-auto rounded-md bg-muted p-3 text-xs">
                          <code>{error.output}</code>
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              )}

              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => onOpenChange(false)}
                >
                  Cancel
                </Button>
                <Button type="button" onClick={submit} disabled={!canSubmit}>
                  {error ? 'Retry' : 'Publish'}
                </Button>
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
