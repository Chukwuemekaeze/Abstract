// Delete the signed-in user's whole account. This is the backend-driven counterpart
// to Clerk's (now disabled) self-serve deletion: the request goes to our backend,
// which tears down every server the proper way (removing Abstract's key from each
// VPS), purges the DB row, then deletes the Clerk user.
//
// A type-to-confirm gate guards the irreversible action. On success the Clerk
// session is gone, so we clear the query cache and sign out (ClerkProvider's
// afterSignOutUrl sends the user to /sign-in). If a server can't be torn down the
// backend returns a 409 naming it; we surface that and keep the user signed in so
// they can resolve the server and retry.

import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useClerk } from '@clerk/clerk-react'
import { AlertTriangle, Loader2 } from 'lucide-react'

import {
  extractAccountDeletionError,
  useDeleteAccountMutation,
} from '@/api/account'
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
import { useDeleteAccountDialogStore } from '@/store/delete-account-dialog'

// The exact phrase the user must type to arm the delete button.
const CONFIRM_PHRASE = 'delete my account'

export function DeleteAccountDialog() {
  const open = useDeleteAccountDialogStore((s) => s.open)
  if (!open) return null
  return <DeleteAccountDialogOpen />
}

function DeleteAccountDialogOpen() {
  const close = useDeleteAccountDialogStore((s) => s.close)
  const qc = useQueryClient()
  const { signOut } = useClerk()
  const del = useDeleteAccountMutation()

  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const pending = del.isPending
  const confirmed = confirm.trim().toLowerCase() === CONFIRM_PHRASE

  const runDelete = async () => {
    setError(null)
    try {
      await del.mutateAsync()
      // The account (and its Clerk session) is gone. Drop all cached data and sign
      // out; ClerkProvider's afterSignOutUrl redirects to /sign-in.
      qc.clear()
      await signOut()
    } catch (err) {
      setError(extractAccountDeletionError(err))
    }
  }

  return (
    <Dialog open onOpenChange={(v) => !pending && !v && close()}>
      <DialogContent showCloseButton={!pending} className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Delete your account?</DialogTitle>
          <DialogDescription>
            This permanently deletes your account and everything in it. This cannot
            be undone.
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
          <p className="mb-2 flex items-center gap-2 font-medium text-destructive">
            <AlertTriangle className="size-4" /> Before you delete:
          </p>
          <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
            <li>
              Every server you registered will be torn down: all projects deleted and
              Abstract removed from each VPS (its SSH key stripped, password and root
              login restored).
            </li>
            <li>
              If a server can&apos;t be reached, deletion stops and tells you which
              one — resolve or delete that server, then try again.
            </li>
            <li>Your login is deleted. You&apos;ll be signed out immediately.</li>
          </ul>
        </div>

        {error && (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        )}

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="confirm-delete-account">
            Type <span className="font-mono">{CONFIRM_PHRASE}</span> to confirm
          </Label>
          <Input
            id="confirm-delete-account"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={pending}
            autoComplete="off"
            placeholder={CONFIRM_PHRASE}
          />
        </div>

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
            disabled={pending || !confirmed}
          >
            {pending && <Loader2 className="mr-2 size-4 animate-spin" />}
            Delete account
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
