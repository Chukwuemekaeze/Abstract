// The Quick harden control. Runs the standard hardening sequence in one atomic
// backend transaction.
//
// If the server already has a sudo user, Quick harden fires immediately reusing that
// username. Otherwise it opens a small dialog to collect the sudo username (default
// "deploy"). While the sequence runs the dialog stays open with a progress message;
// success closes it with a toast, failure keeps it open with the captured output and
// a Retry button.

import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractHardeningError } from '@/api/client'
import { type Server, useQuickHardenMutation } from '@/api/servers'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

const USERNAME_PATTERN = /^[a-z_][a-z0-9_-]*$/

export function QuickHardenSection({
  server,
  disabled,
}: {
  server: Server
  disabled: boolean
}) {
  const quickHarden = useQuickHardenMutation()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [username, setUsername] = useState('deploy')
  const [failure, setFailure] = useState<{ message: string; output: string | null } | null>(
    null,
  )

  const run = async (sudoUserName: string) => {
    setFailure(null)
    try {
      await quickHarden.mutateAsync({
        serverId: server.id,
        body: { sudo_user_name: sudoUserName },
      })
      toast.success('Server hardened.')
      setDialogOpen(false)
    } catch (err) {
      setFailure(extractHardeningError(err))
    }
  }

  const onQuickHardenClick = () => {
    if (server.sudo_user_name) {
      // Already has a sudo user: reuse it, skip the dialog.
      void run(server.sudo_user_name)
    } else {
      setFailure(null)
      setDialogOpen(true)
    }
  }

  const usernameValid = USERNAME_PATTERN.test(username)
  const running = quickHarden.isPending

  return (
    <div className="rounded-lg border bg-card p-5">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Quick harden</h2>
          <p className="text-sm text-muted-foreground">
            Run the standard sequence: update, base packages, Docker, sudo user,
            firewall, swap, and disable root login.
          </p>
        </div>
        <Button onClick={onQuickHardenClick} disabled={disabled || running}>
          {running && <Loader2 className="mr-2 size-4 animate-spin" />}
          Quick harden
        </Button>
      </div>

      <Dialog open={dialogOpen} onOpenChange={(open) => !running && setDialogOpen(open)}>
        <DialogContent showCloseButton={!running}>
          <DialogHeader>
            <DialogTitle>Quick harden</DialogTitle>
            <DialogDescription>
              Choose the non-root sudo user to create. This user gets the app deploy
              key and passwordless sudo.
            </DialogDescription>
          </DialogHeader>

          {running ? (
            <div className="flex items-center gap-3 py-4 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              Hardening in progress. This may take several minutes.
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <Label htmlFor="sudo-username">Sudo username</Label>
              <Input
                id="sudo-username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="deploy"
                autoFocus
              />
              {!usernameValid && (
                <p className="text-xs text-destructive">
                  Use a lowercase Linux username (letters, digits, _ and -).
                </p>
              )}
              {failure && (
                <div className="mt-2">
                  <p className="text-sm text-destructive">{failure.message}</p>
                  {failure.output && (
                    <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-muted p-3 text-xs">
                      <code>{failure.output}</code>
                    </pre>
                  )}
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button
              onClick={() => run(username)}
              disabled={running || !usernameValid}
              variant={failure ? 'destructive' : 'default'}
            >
              {failure ? 'Retry' : 'Start'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
