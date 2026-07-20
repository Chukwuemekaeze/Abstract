// Zustand store for the cancel-registration confirmation dialog. A single dialog
// instance lives on the servers list and the server detail page; the "Cancel
// registration" button opens it with the target (still-pending) server id. Mirrors
// the delete-server dialog store: open flag, the payload, and openWith/close.

import { create } from 'zustand'

interface CancelRegistrationDialogState {
  open: boolean
  serverId: string | null
  openWith: (serverId: string) => void
  close: () => void
}

export const useCancelRegistrationDialogStore =
  create<CancelRegistrationDialogState>((set) => ({
    open: false,
    serverId: null,
    openWith: (serverId) => set({ open: true, serverId }),
    close: () => set({ open: false, serverId: null }),
  }))
