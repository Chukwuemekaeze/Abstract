// Re-register a server whose SSH host key changed (status key_mismatch), the recovery
// path after a VPS rebuild/replacement. This re-establishes trust deliberately, never
// silently: we re-probe the current host, show the NEW fingerprint for out-of-band
// confirmation, take the current VPS password, and install a fresh app key.
//
//   intro -> reprobe -> awaiting_confirmation -> install_key -> done
//
// The intro step warns that re-registering wipes Abstract's record of the projects and
// deployments that used to live on this box (the rebuild already destroyed them) and
// resets the hardening state. The dialog mounts only while open, so state starts fresh
// on every open. On success the row is verified again and the page reflects it.

import { useEffect, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage, isPasswordChangeRequired } from '@/api/client'
import {
  useInstallKeyMutation,
  useReprobeServerMutation,
  useServer,
} from '@/api/servers'
import { FingerprintConfirm } from '@/components/FingerprintConfirm'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useReRegisterServerDialogStore } from '@/store/reregister-server-dialog'

type Step =
  | 'intro'
  | 'reprobing'
  | 'awaiting_confirmation'
  | 'installing'
  | 'done'
  | 'failed'

export function ReRegisterServerDialog() {
  const open = useReRegisterServerDialogStore((s) => s.open)
  const serverId = useReRegisterServerDialogStore((s) => s.serverId)
  if (!open || !serverId) return null
  return <ReRegisterDialogOpen serverId={serverId} />
}

function ReRegisterDialogOpen({ serverId }: { serverId: string }) {
  const close = useReRegisterServerDialogStore((s) => s.close)
  const { data: server } = useServer(serverId)

  const reprobe = useReprobeServerMutation()
  const install = useInstallKeyMutation()

  const [step, setStep] = useState<Step>('intro')
  const [username, setUsername] = useState('root')
  const [fingerprint, setFingerprint] = useState('')
  const [password, setPassword] = useState('')
  const [disablePasswordAuth, setDisablePasswordAuth] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Set when the VPS forces a password change on first login (expired password). We
  // stay on the confirmation step and reveal a "New password" field for the retry.
  const [passwordChangeRequired, setPasswordChangeRequired] = useState(false)
  const [newPassword, setNewPassword] = useState('')
  const [confirmNewPassword, setConfirmNewPassword] = useState('')

  // Client-side validation for the new password (only matters once the VPS forces a
  // change). The server is the real authority, but catching mismatches/weak passwords
  // here avoids setting a root password the user cannot repeat.
  const newPasswordError = !passwordChangeRequired
    ? null
    : newPassword.length > 0 && newPassword.length < 8
      ? 'Use at least 8 characters.'
      : newPassword.length > 0 && newPassword === password
        ? 'The new password must differ from the current one.'
        : confirmNewPassword.length > 0 && confirmNewPassword !== newPassword
          ? 'The passwords do not match.'
          : null
  const newPasswordValid =
    !passwordChangeRequired ||
    (newPassword.length >= 8 &&
      newPassword !== password &&
      newPassword === confirmNewPassword)

  // On the done step, auto close after a short success pause.
  useEffect(() => {
    if (step !== 'done') return
    const timer = setTimeout(() => close(), 2000)
    return () => clearTimeout(timer)
  }, [step, close])

  // Step one: re-probe the current host and read its new key.
  const handleReprobe = async () => {
    setStep('reprobing')
    setError(null)
    try {
      const result = await reprobe.mutateAsync({ serverId, body: { username } })
      setFingerprint(result.fingerprint_sha256)
      setStep('awaiting_confirmation')
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not reach the host.'))
      setStep('failed')
    }
  }

  // Step two: install the fresh app key with the confirmed fingerprint + password.
  const handleInstall = async () => {
    setStep('installing')
    setError(null)
    try {
      await install.mutateAsync({
        serverId,
        body: {
          password,
          disable_password_auth: disablePasswordAuth,
          ...(passwordChangeRequired ? { new_password: newPassword } : {}),
        },
      })
      setStep('done')
      toast.success('Server re-registered and key installed.')
    } catch (err) {
      if (isPasswordChangeRequired(err)) {
        // The VPS demands a password change. Reveal the new-password field and stay on
        // the confirmation step so the user can retry without losing their place.
        setPasswordChangeRequired(true)
        setStep('awaiting_confirmation')
        return
      }
      setError(extractErrorMessage(err, 'Key installation failed.'))
      setStep('failed')
    }
  }

  // From the failed step, retry. If the re-probe already succeeded (row is pending with
  // the new key stored), go back to fingerprint confirmation and re-run the install;
  // otherwise return to the intro to re-probe.
  const handleRetry = () => {
    setError(null)
    setStep(fingerprint ? 'awaiting_confirmation' : 'intro')
  }

  const busy = step === 'reprobing' || step === 'installing'

  return (
    <Dialog open onOpenChange={(v) => !busy && !v && close()}>
      <DialogContent showCloseButton={!busy} className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Re-register {server?.name ?? 'server'}</DialogTitle>
          <DialogDescription>
            The host key changed, so Abstract can no longer verify this server. Confirm
            the new fingerprint and install a fresh deploy key to re-establish trust.
          </DialogDescription>
        </DialogHeader>

        {/* Step: intro — warn about the stale-state wipe and collect the username. */}
        {step === 'intro' && (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              handleReprobe()
            }}
          >
            <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
              <p className="mb-2 flex items-center gap-2 font-medium text-destructive">
                <AlertTriangle className="size-4" /> This is a fresh registration
              </p>
              <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                <li>
                  A rebuild wipes the VPS. Abstract's record of the projects and
                  deployments on this server is now stale and will be removed — it can no
                  longer be trusted as healthy.
                </li>
                <li>
                  Hardening state (sudo user, firewall, Docker, nginx, swap) is reset;
                  you will need to re-harden and re-deploy.
                </li>
                <li>
                  You will confirm the new host key fingerprint and enter the current VPS
                  password, exactly like the first registration.
                </li>
              </ul>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="reregister-username">Username</Label>
              <Input
                id="reregister-username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="root"
                autoComplete="off"
                required
              />
              <p className="text-xs text-muted-foreground">
                A rebuilt VPS is usually reachable as root with a password again.
              </p>
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={close}>
                Cancel
              </Button>
              <Button type="submit" disabled={!username}>
                Continue
              </Button>
            </div>
          </form>
        )}

        {/* Step: reprobing */}
        {step === 'reprobing' && (
          <div className="flex items-center gap-3 py-8">
            <Loader2 className="size-5 animate-spin" />
            <span>Reaching the host and reading its new key...</span>
          </div>
        )}

        {/* Step: awaiting_confirmation — show the NEW fingerprint, collect password. */}
        {step === 'awaiting_confirmation' && (
          <FingerprintConfirm
            fingerprint={fingerprint}
            onConfirm={handleInstall}
            onCancel={close}
            busy={!password || !newPasswordValid}
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="reregister-password">
                {passwordChangeRequired ? 'Current (expired) password' : 'Password'}
              </Label>
              <Input
                id="reregister-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Used once to install the key, never stored"
                autoComplete="off"
                required
              />
            </div>
            {passwordChangeRequired && (
              <div className="flex flex-col gap-2">
                <div className="rounded-md border border-yellow-500/40 bg-yellow-500/5 p-3 text-sm text-muted-foreground">
                  This server requires a password change on first login (its password
                  has expired). Enter a new root password to continue — Abstract sets it
                  during login, then installs its key.
                </div>
                <Label htmlFor="reregister-new-password">New root password</Label>
                <Input
                  id="reregister-new-password"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  placeholder="A new password for the root account"
                  autoComplete="off"
                  required
                />
                <Label htmlFor="reregister-confirm-new-password">
                  Confirm new password
                </Label>
                <Input
                  id="reregister-confirm-new-password"
                  type="password"
                  value={confirmNewPassword}
                  onChange={(e) => setConfirmNewPassword(e.target.value)}
                  placeholder="Retype the new password"
                  autoComplete="off"
                  required
                />
                {newPasswordError && (
                  <p className="text-destructive text-xs">{newPasswordError}</p>
                )}
              </div>
            )}
            <div className="flex items-center gap-2">
              <Checkbox
                id="reregister-disable-password-auth"
                checked={disablePasswordAuth}
                onCheckedChange={(checked) =>
                  setDisablePasswordAuth(checked === true)
                }
              />
              <Label
                htmlFor="reregister-disable-password-auth"
                className="font-normal"
              >
                Disable password authentication after install (recommended)
              </Label>
            </div>
          </FingerprintConfirm>
        )}

        {/* Step: installing */}
        {step === 'installing' && (
          <div className="flex items-center gap-3 py-8">
            <Loader2 className="size-5 animate-spin" />
            <span>Installing public key...</span>
          </div>
        )}

        {/* Step: done */}
        {step === 'done' && (
          <div className="py-8 text-center">
            <p className="text-lg font-medium">Server re-registered</p>
            <p className="text-muted-foreground text-sm">
              This dialog will close shortly.
            </p>
          </div>
        )}

        {/* Step: failed */}
        {step === 'failed' && (
          <div className="flex flex-col gap-4 py-4">
            <p className="text-destructive text-sm">{error}</p>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={close}>
                Close
              </Button>
              <Button type="button" onClick={handleRetry}>
                Retry
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
