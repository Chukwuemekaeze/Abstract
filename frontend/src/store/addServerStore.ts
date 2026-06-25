// Zustand store driving the Add Server dialog as an explicit state machine.
//
// The flow has distinct steps and the dialog renders different content per step.
// Keeping this state outside the component means the dialog can be opened from
// anywhere (page header, empty state CTA) and the step transitions are testable
// and centralized.

import { create } from 'zustand'

// idle                -> dialog closed
// form                -> collecting connection details
// probing             -> probe request in flight
// awaiting_confirmation -> showing fingerprint, waiting for the user to confirm
// installing          -> install_key request in flight
// done                -> success, dialog auto closes shortly after
// failed              -> an error occurred, show message and offer retry
export type AddServerStep =
  | 'idle'
  | 'form'
  | 'probing'
  | 'awaiting_confirmation'
  | 'installing'
  | 'done'
  | 'failed'

export interface AddServerFormData {
  name: string
  host: string
  port: number
  username: string
  password: string
  disable_password_auth: boolean
}

// Data returned by the probe step that the confirmation and install steps need.
export interface PendingServer {
  id: string
  fingerprint: string
  app_public_key: string
}

const initialFormData: AddServerFormData = {
  name: '',
  host: '',
  port: 22,
  username: 'root',
  password: '',
  disable_password_auth: true,
}

interface AddServerState {
  step: AddServerStep
  formData: AddServerFormData
  pendingServer: PendingServer | null
  error: string | null

  // Open the dialog at the form step. Close and reset everything.
  open: () => void
  close: () => void

  // Granular setters used by the dialog as it walks through the flow.
  setFormData: (patch: Partial<AddServerFormData>) => void
  setStep: (step: AddServerStep) => void
  setPendingServer: (pending: PendingServer | null) => void
  setError: (error: string | null) => void
  reset: () => void
}

export const useAddServerStore = create<AddServerState>((set) => ({
  step: 'idle',
  formData: initialFormData,
  pendingServer: null,
  error: null,

  open: () => set({ step: 'form', error: null }),
  // Closing fully resets so the next open starts clean.
  close: () =>
    set({
      step: 'idle',
      formData: initialFormData,
      pendingServer: null,
      error: null,
    }),

  setFormData: (patch) =>
    set((state) => ({ formData: { ...state.formData, ...patch } })),
  setStep: (step) => set({ step }),
  setPendingServer: (pendingServer) => set({ pendingServer }),
  setError: (error) => set({ error }),
  reset: () =>
    set({
      step: 'idle',
      formData: initialFormData,
      pendingServer: null,
      error: null,
    }),
}))
