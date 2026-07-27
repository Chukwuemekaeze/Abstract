// Zustand store for the delete-account confirmation dialog. A single dialog
// instance is mounted in the Header; the "Delete account" item in the UserButton
// menu opens it. Mirrors the other small dialog stores: open flag + open/close.

import { create } from 'zustand'

interface DeleteAccountDialogState {
  open: boolean
  openDialog: () => void
  close: () => void
}

export const useDeleteAccountDialogStore = create<DeleteAccountDialogState>(
  (set) => ({
    open: false,
    openDialog: () => set({ open: true }),
    close: () => set({ open: false }),
  }),
)
