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

// The subset of a persisted server we need to resume its registration. Matches the
// fields the servers list/detail already have on hand (see api/servers Server).
export interface ResumableServer {
  id: string
  name: string
  host: string
  port: number
  username: string
  fingerprint_sha256: string | null
}

interface AddServerState {
  step: AddServerStep
  formData: AddServerFormData
  pendingServer: PendingServer | null
  error: string | null

  // Open the dialog at the form step. Close and reset everything.
  open: () => void
  close: () => void

  // Resume a still-pending registration: skip the form/probe and jump straight to
  // fingerprint confirmation with the connection details prefilled. The password is
  // deliberately left blank so the user re-enters it (we never persist it).
  resume: (server: ResumableServer) => void

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
  // Closing fully resets so the next open starts clean. It never touches the pending
  // row on the backend: dismissing the dialog leaves the registration intact, and
  // only the explicit "Cancel registration" action deletes it.
  close: () =>
    set({
      step: 'idle',
      formData: initialFormData,
      pendingServer: null,
      error: null,
    }),

  resume: (server) =>
    set({
      step: 'awaiting_confirmation',
      formData: {
        name: server.name,
        host: server.host,
        port: server.port,
        username: server.username,
        password: '',
        disable_password_auth: true,
      },
      pendingServer: {
        id: server.id,
        fingerprint: server.fingerprint_sha256 ?? '',
        // Only ever displayed via the fingerprint; the install step reads the key
        // server side, so an empty value here is fine on resume.
        app_public_key: '',
      },
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
