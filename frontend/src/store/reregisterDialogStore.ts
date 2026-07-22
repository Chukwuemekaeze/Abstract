// Zustand store for the re-registration dialog. A single dialog instance lives on the
// server detail page; the key-mismatch banner's "Re-register this server" button opens
// it with the target server id. Mirrors the other small dialog stores: open flag, the
// payload, and openWith/close.

import { create } from 'zustand'

interface ReregisterDialogState {
  open: boolean
  serverId: string | null
  openWith: (serverId: string) => void
  close: () => void
}

export const useReregisterDialogStore = create<ReregisterDialogState>((set) => ({
  open: false,
  serverId: null,
  openWith: (serverId) => set({ open: true, serverId }),
  close: () => set({ open: false, serverId: null }),
}))
