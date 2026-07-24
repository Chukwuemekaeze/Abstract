// The re-registration dialog for a server whose host key changed (VPS rebuilt). Two
// steps mirror the original registration: confirm the new fingerprint, then enter the
// server's root password. The user never chooses a mode and never sees the words "SSH
// key"; a provider-forced password change is handled transparently on the backend.
//
// State machine: probing -> confirm -> password -> submitting -> done | failed.
// While submitting, the server's reregistration_state is polled to show friendly
// progress text.

import { useEffect, useRef, useState } from 'react'
import { Check, Copy, Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { apiClient } from '@/api/client'
import {
  extractReregistrationError,
  serverKeys,
  useReregisterCompleteMutation,
  useReregisterProbeMutation,
  type ReregistrationError,
  type ReregistrationState,
  type Server,
} from '@/api/servers'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
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
import { useReregisterDialogStore } from '@/store/reregisterDialogStore'

type Step = 'probing' | 'confirm' | 'password' | 'submitting' | 'done' | 'failed'

// Map the persisted backend state to friendly, non-technical progress copy.
function progressText(state: ReregistrationState | undefined): string {
  switch (state) {
    case 'exchanging':
      return 'Your provider required a password reset, handling it'
    case 'verifying':
      return 'Securing the connection'
    case 'installing_key':
      return 'Finishing up'
    default:
      return 'Connecting'
  }
}

export function ReregisterDialog() {
  const open = useReregisterDialogStore((s) => s.open)
  const serverId = useReregisterDialogStore((s) => s.serverId)
  if (!open || !serverId) return null
  return <ReregisterDialogOpen serverId={serverId} />
}

function ReregisterDialogOpen({ serverId }: { serverId: string }) {
  const close = useReregisterDialogStore((s) => s.close)

  const probe = useReregisterProbeMutation()
  const complete = useReregisterCompleteMutation()

  const [step, setStep] = useState<Step>('probing')
  const [fingerprint, setFingerprint] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<ReregistrationError | null>(null)
  // Which phase failed, so a retryable failure re-runs the right action.
  const [failedPhase, setFailedPhase] = useState<'probe' | 'complete'>('complete')

  // Poll the server while the backend runs the exchange so the progress text reflects
  // the real persisted state rather than a guess.
  const serverPoll = useQuery({
    queryKey: [...serverKeys.detail(serverId), 'reregister-progress'],
    enabled: step === 'submitting',
    refetchInterval: step === 'submitting' ? 1500 : false,
    queryFn: async (): Promise<Server> => {
      const { data } = await apiClient.get<Server>(`/servers/${serverId}`)
      return data
    },
  })

  // Kick off the probe exactly once when the dialog opens.
  const probeStarted = useRef(false)
  useEffect(() => {
    if (probeStarted.current) return
    probeStarted.current = true
    void runProbe()
    // runProbe only touches local/mutation state; safe to omit from deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const runProbe = async () => {
    setStep('probing')
    setError(null)
    try {
      const result = await probe.mutateAsync(serverId)
      setFingerprint(result.fingerprint_sha256)
      setStep('confirm')
    } catch (err) {
      setFailedPhase('probe')
      setError(extractReregistrationError(err, 'Could not reach the server.'))
      setStep('failed')
    }
  }

  const runComplete = async () => {
    setStep('submitting')
    setError(null)
    try {
      await complete.mutateAsync({ serverId, password })
      setStep('done')
    } catch (err) {
      setFailedPhase('complete')
      setError(extractReregistrationError(err))
      setStep('failed')
    }
  }

  const copyFingerprint = async () => {
    if (!fingerprint) return
    await navigator.clipboard.writeText(fingerprint)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const handleOpenChange = (next: boolean) => {
    if (!next) close()
  }

  return (
    <Dialog open onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Re-register this server</DialogTitle>
          <DialogDescription>
            Confirm the rebuilt server's fingerprint, then enter its root password.
          </DialogDescription>
        </DialogHeader>

        {step === 'probing' && (
          <div className="flex items-center gap-3 py-8">
            <Loader2 className="size-5 animate-spin" />
            <span>Reaching the server and reading its new key...</span>
          </div>
        )}

        {step === 'confirm' && fingerprint && (
          <div className="flex flex-col gap-4">
            <Alert>
              <AlertTitle>Verify the host key fingerprint</AlertTitle>
              <AlertDescription>
                Compare this fingerprint to the one shown in your VPS provider's console
                before continuing. They must match.
              </AlertDescription>
            </Alert>

            <div className="flex items-center gap-2 rounded-md border bg-muted p-3">
              <code className="flex-1 break-all font-mono text-sm">{fingerprint}</code>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={copyFingerprint}
                aria-label="Copy fingerprint"
              >
                {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
              </Button>
            </div>

            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={close}>
                Cancel
              </Button>
              <Button type="button" onClick={() => setStep('password')}>
                Fingerprint matches, continue
              </Button>
            </div>
          </div>
        )}

        {step === 'password' && (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              void runComplete()
            }}
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="reregister-password">
                Enter your server's root password
              </Label>
              <Input
                id="reregister-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Used to re-establish access, never stored"
                autoComplete="off"
                required
              />
              <p className="text-muted-foreground text-sm">
                This is the password you set when rebuilding, or the one your provider
                emailed you.
              </p>
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={close}>
                Cancel
              </Button>
              <Button type="submit" disabled={!password}>
                Re-register
              </Button>
            </div>
          </form>
        )}

        {step === 'submitting' && (
          <div className="flex items-center gap-3 py-8">
            <Loader2 className="size-5 animate-spin" />
            <span>{progressText(serverPoll.data?.reregistration_state)}...</span>
          </div>
        )}

        {step === 'done' && (
          <div className="flex flex-col gap-4 py-4">
            <div className="text-center">
              <p className="text-lg font-medium">Server re-registered</p>
              <p className="text-muted-foreground text-sm">
                Access is restored, but the server is no longer hardened.
              </p>
            </div>
            <Alert>
              <AlertTitle>Next steps</AlertTitle>
              <AlertDescription>
                Run Quick Harden to re-secure the server, then add your projects again.
                The rebuild wiped the old projects and their deployments, so this server
                starts fresh.
              </AlertDescription>
            </Alert>
            <div className="flex justify-end">
              <Button type="button" onClick={close}>
                Done
              </Button>
            </div>
          </div>
        )}

        {step === 'failed' && error && (
          <div className="flex flex-col gap-4 py-4">
            <p className="text-destructive text-sm">{error.message}</p>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={close}>
                Close
              </Button>
              {error.retryable && (
                <Button
                  type="button"
                  onClick={() =>
                    void (failedPhase === 'probe' ? runProbe() : runComplete())
                  }
                >
                  Try again
                </Button>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
