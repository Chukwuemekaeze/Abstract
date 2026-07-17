// Zustand store for the rollback confirmation dialog. A single dialog instance
// lives on the project card; version history rows open it with the target run
// id. Mirrors the small dialog stores elsewhere: open flag, the payload, and
// openWith/close.

import { create } from 'zustand'

interface RollbackDialogState {
  open: boolean
  targetRunId: string | null
  openWith: (runId: string) => void
  close: () => void
}

export const useRollbackDialogStore = create<RollbackDialogState>((set) => ({
  open: false,
  targetRunId: null,
  openWith: (runId) => set({ open: true, targetRunId: runId }),
  close: () => set({ open: false, targetRunId: null }),
}))
