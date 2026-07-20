// Zustand store for the delete-server confirmation dialog. A single dialog
// instance lives on the server detail page; the "Delete server" button opens it
// with the target server id. Mirrors the small dialog stores elsewhere: open
// flag, the payload, and openWith/close.

import { create } from 'zustand'

interface DeleteServerDialogState {
  open: boolean
  serverId: string | null
  openWith: (serverId: string) => void
  close: () => void
}

export const useDeleteServerDialogStore = create<DeleteServerDialogState>(
  (set) => ({
    open: false,
    serverId: null,
    openWith: (serverId) => set({ open: true, serverId }),
    close: () => set({ open: false, serverId: null }),
  }),
)
