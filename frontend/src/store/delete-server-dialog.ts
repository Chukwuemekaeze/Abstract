// Zustand store for the delete-server confirmation dialog. A single dialog
// instance lives on the server detail page; the "Delete server" button opens it
// with the target server id. Mirrors the small dialog stores elsewhere: open
// flag, the payload, and openWith/close.

import { create } from 'zustand'

interface DeleteServerDialogState {
  open: boolean
  serverId: string | null
  // Records-only purge for a rebuilt (key_mismatch) server: delete Abstract's record
  // without any SSH teardown. Set by the "Remove server record" button.
  recordsOnly: boolean
  openWith: (serverId: string, opts?: { recordsOnly?: boolean }) => void
  close: () => void
}

export const useDeleteServerDialogStore = create<DeleteServerDialogState>(
  (set) => ({
    open: false,
    serverId: null,
    recordsOnly: false,
    openWith: (serverId, opts) =>
      set({ open: true, serverId, recordsOnly: opts?.recordsOnly ?? false }),
    close: () => set({ open: false, serverId: null, recordsOnly: false }),
  }),
)
