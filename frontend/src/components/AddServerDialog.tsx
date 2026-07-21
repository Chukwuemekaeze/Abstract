// The Add Server dialog. It renders different content depending on the current
// step in the Zustand state machine, and it drives the two backend calls:
//   form  -> probe  -> awaiting_confirmation -> install_key -> done
// Errors at either step move us to the failed step with a retry affordance.

import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage, isPasswordChangeRequired } from '@/api/client'
import {
  useInstallKeyMutation,
  useProbeServerMutation,
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
import { useAddServerStore } from '@/store/addServerStore'

export function AddServerDialog() {
  const {
    step,
    formData,
    pendingServer,
    error,
    close,
    setFormData,
    setStep,
    setPendingServer,
    setError,
  } = useAddServerStore()

  const probeMutation = useProbeServerMutation()
  const installMutation = useInstallKeyMutation()

  // Set when the VPS forces a password change on first login (expired password). We
  // stay on the confirmation step and reveal a "New password" field for the retry.
  // Kept local so the new password is never persisted, like the password itself.
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
      : newPassword.length > 0 && newPassword === formData.password
        ? 'The new password must differ from the current one.'
        : confirmNewPassword.length > 0 && confirmNewPassword !== newPassword
          ? 'The passwords do not match.'
          : null
  const newPasswordValid =
    !passwordChangeRequired ||
    (newPassword.length >= 8 &&
      newPassword !== formData.password &&
      newPassword === confirmNewPassword)

  // The dialog is open for every step except idle.
  const isOpen = step !== 'idle'

  // Reset the transient password-change state and close. Kept out of an effect so the
  // reset is an explicit event, not a render side effect.
  const handleClose = () => {
    setPasswordChangeRequired(false)
    setNewPassword('')
    setConfirmNewPassword('')
    close()
  }

  // On the done step, auto close after a short success pause.
  useEffect(() => {
    if (step !== 'done') return
    const timer = setTimeout(() => handleClose(), 2000)
    return () => clearTimeout(timer)
    // handleClose only touches setState/close, all stable for this purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step])

  // Step one: probe the host, then move to fingerprint confirmation.
  const handleProbe = async () => {
    setStep('probing')
    setError(null)
    setPasswordChangeRequired(false)
    setNewPassword('')
    setConfirmNewPassword('')
    try {
      const result = await probeMutation.mutateAsync({
        name: formData.name,
        host: formData.host,
        port: formData.port,
        username: formData.username,
      })
      setPendingServer({
        id: result.server_id,
        fingerprint: result.fingerprint_sha256,
        app_public_key: result.app_public_key,
      })
      setStep('awaiting_confirmation')
    } catch (err) {
      setError(extractErrorMessage(err, 'Could not reach the host.'))
      setStep('failed')
    }
  }

  // Step two: install the app key on the confirmed server.
  const handleInstall = async () => {
    if (!pendingServer) return
    setStep('installing')
    setError(null)
    try {
      await installMutation.mutateAsync({
        serverId: pendingServer.id,
        body: {
          password: formData.password,
          disable_password_auth: formData.disable_password_auth,
          ...(passwordChangeRequired ? { new_password: newPassword } : {}),
        },
      })
      setStep('done')
      toast.success('Server verified and key installed.')
    } catch (err) {
      if (isPasswordChangeRequired(err)) {
        // The VPS demands a password change on first login. Reveal the new-password
        // field and stay on the confirmation step so the user can retry in place.
        setPasswordChangeRequired(true)
        setStep('awaiting_confirmation')
        return
      }
      setError(extractErrorMessage(err, 'Key installation failed.'))
      setStep('failed')
    }
  }

  // From the failed step, retry. If the probe already created a pending row, go back
  // to fingerprint confirmation and re-run the install (re-probing from the form would
  // create a second pending row). Only a failure before any probe returns to the form.
  const handleRetry = () => {
    setError(null)
    setStep(pendingServer ? 'awaiting_confirmation' : 'form')
  }

  // Only allow closing via the controlled handler so state always resets.
  const handleOpenChange = (open: boolean) => {
    if (!open) handleClose()
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add a server</DialogTitle>
          <DialogDescription>
            Register a VPS, verify its host key, and install the deploy key.
          </DialogDescription>
        </DialogHeader>

        {/* Step: form */}
        {step === 'form' && (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              handleProbe()
            }}
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                value={formData.name}
                onChange={(e) => setFormData({ name: e.target.value })}
                placeholder="production-web"
                required
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="host">Host</Label>
              <Input
                id="host"
                value={formData.host}
                onChange={(e) => setFormData({ host: e.target.value })}
                placeholder="203.0.113.10"
                required
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-2">
                <Label htmlFor="port">Port</Label>
                <Input
                  id="port"
                  type="number"
                  value={formData.port}
                  onChange={(e) =>
                    setFormData({ port: Number(e.target.value) || 22 })
                  }
                  required
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="username">Username</Label>
                <Input
                  id="username"
                  value={formData.username}
                  onChange={(e) => setFormData({ username: e.target.value })}
                  required
                />
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={handleClose}>
                Cancel
              </Button>
              <Button type="submit">Continue</Button>
            </div>
          </form>
        )}

        {/* Step: probing */}
        {step === 'probing' && (
          <div className="flex items-center gap-3 py-8">
            <Loader2 className="size-5 animate-spin" />
            <span>Reaching the host and reading its key...</span>
          </div>
        )}

        {/* Step: awaiting_confirmation. The password is collected here, right before
            it is used, so the same step serves a fresh registration and a resumed one
            (where the form/probe were already done). It is never persisted. */}
        {step === 'awaiting_confirmation' && pendingServer && (
          <FingerprintConfirm
            fingerprint={pendingServer.fingerprint}
            onConfirm={handleInstall}
            onCancel={handleClose}
            busy={!formData.password || !newPasswordValid}
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">
                {passwordChangeRequired ? 'Current (expired) password' : 'Password'}
              </Label>
              <Input
                id="password"
                type="password"
                value={formData.password}
                onChange={(e) => setFormData({ password: e.target.value })}
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
                <Label htmlFor="new-password">New root password</Label>
                <Input
                  id="new-password"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  placeholder="A new password for the root account"
                  autoComplete="off"
                  required
                />
                <Label htmlFor="confirm-new-password">Confirm new password</Label>
                <Input
                  id="confirm-new-password"
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
                id="disable_password_auth"
                checked={formData.disable_password_auth}
                onCheckedChange={(checked) =>
                  setFormData({ disable_password_auth: checked === true })
                }
              />
              <Label htmlFor="disable_password_auth" className="font-normal">
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
            <p className="text-lg font-medium">Server verified</p>
            <p className="text-muted-foreground text-sm">This dialog will close shortly.</p>
          </div>
        )}

        {/* Step: failed */}
        {step === 'failed' && (
          <div className="flex flex-col gap-4 py-4">
            <p className="text-destructive text-sm">{error}</p>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={handleClose}>
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
