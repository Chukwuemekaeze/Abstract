// Zustand store driving the New Project dialog as an explicit state machine,
// mirroring addServerStore.
//
// The dialog can be opened from the projects page (server freely selectable)
// or from a server detail page (initialServerId preselects and locks the
// server field).

import { create } from 'zustand'

// idle       -> dialog closed
// form       -> collecting name, server, repo
// submitting -> create request in flight (deploy key + clone; can take a while)
// done       -> success, dialog auto closes shortly after
// failed     -> an error occurred, show message and offer retry
export type NewProjectStep = 'idle' | 'form' | 'submitting' | 'done' | 'failed'

export interface NewProjectFormData {
  name: string
  serverId: string
  repoId: number | null
  repoFullName: string
}

const initialFormData: NewProjectFormData = {
  name: '',
  serverId: '',
  repoId: null,
  repoFullName: '',
}

interface NewProjectState {
  step: NewProjectStep
  formData: NewProjectFormData
  // When opened from a server detail page, the server is preselected and the
  // select is disabled.
  initialServerId: string | null
  error: string | null
  // Raw shell output captured by the backend on clone failures (502 detail),
  // shown in a collapsible block like the hardening cards do.
  errorOutput: string | null

  open: (opts?: { initialServerId?: string }) => void
  close: () => void

  setFormData: (patch: Partial<NewProjectFormData>) => void
  setStep: (step: NewProjectStep) => void
  setError: (error: string | null, output?: string | null) => void
}

export const useNewProjectStore = create<NewProjectState>((set) => ({
  step: 'idle',
  formData: initialFormData,
  initialServerId: null,
  error: null,
  errorOutput: null,

  open: (opts) =>
    set({
      step: 'form',
      error: null,
      errorOutput: null,
      initialServerId: opts?.initialServerId ?? null,
      formData: {
        ...initialFormData,
        serverId: opts?.initialServerId ?? '',
      },
    }),
  // Closing fully resets so the next open starts clean.
  close: () =>
    set({
      step: 'idle',
      formData: initialFormData,
      initialServerId: null,
      error: null,
      errorOutput: null,
    }),

  setFormData: (patch) =>
    set((state) => ({ formData: { ...state.formData, ...patch } })),
  setStep: (step) => set({ step }),
  setError: (error, output = null) => set({ error, errorOutput: output }),
}))
