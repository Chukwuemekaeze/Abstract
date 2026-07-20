// Zustand store for the re-register-server dialog. A single instance lives on the
// server detail page; the "Re-register this server" button (shown only when the server
// is key_mismatch) opens it with the target server id. Mirrors the cancel-registration
// and delete-server dialog stores: open flag, the payload, and openWith/close.

import { create } from 'zustand'

interface ReRegisterServerDialogState {
  open: boolean
  serverId: string | null
  openWith: (serverId: string) => void
  close: () => void
}

export const useReRegisterServerDialogStore = create<ReRegisterServerDialogState>(
  (set) => ({
    open: false,
    serverId: null,
    openWith: (serverId) => set({ open: true, serverId }),
    close: () => set({ open: false, serverId: null }),
  }),
)
