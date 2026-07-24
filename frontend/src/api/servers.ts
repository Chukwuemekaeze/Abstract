// TanStack Query hooks for the servers API.
//
// Queries read data and are cached under stable query keys. Mutations change data
// and invalidate the relevant query keys on success so the UI refetches and stays
// consistent. All network calls go through the shared axios client (baseURL '/api').

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'

import { apiClient, extractErrorMessage } from '@/api/client'
import { projectKeys } from '@/api/projects'

// Mirrors the backend server status check constraint.
export type ServerStatus = 'pending_verification' | 'verified' | 'key_mismatch'

// Mirrors the backend reregistration_state machine.
export type ReregistrationState =
  | 'none'
  | 'awaiting_confirmation'
  | 'probing'
  | 'exchanging'
  | 'verifying'
  | 'installing_key'
  | 'done'

// Shape returned by the backend ServerResponse schema. Sensitive fields
// (host_key, encrypted_private_key) are deliberately never sent by the backend,
// so they are absent here too.
export interface Server {
  id: string
  name: string
  host: string
  port: number
  username: string
  status: ServerStatus
  // Live re-registration progress ('none' when idle). The re-register modal polls this
  // to show friendly progress text while /reregister/complete runs.
  reregistration_state: ReregistrationState
  // The single in-flight server-level operation, or null when idle. Currently
  // only 'deleting'. When set, the detail page locks its mutation UI.
  active_operation: 'deleting' | null
  fingerprint_sha256: string | null
  host_key_type: string | null
  password_auth_disabled: boolean
  verification_source: string
  created_at: string
  verified_at: string | null
  // Hardening state.
  sudo_user_name: string | null
  root_login_disabled: boolean
  firewall_enabled: boolean
  docker_installed: boolean
  base_packages_installed: boolean
  nginx_installed: boolean
  swap_configured: boolean
  last_system_update_at: string | null
}

// Returned by POST /servers/probe: the new server id plus the data the user needs
// to confirm the fingerprint and install the app key.
export interface ProbeResponse {
  server_id: string
  fingerprint_sha256: string
  app_public_key: string
}

// Returned by POST /servers/{id}/smoke_test.
export interface CommandResult {
  stdout: string
  stderr: string
  exit_status: number
}

export interface ProbeRequest {
  name: string
  host: string
  port: number
  username: string
}

export interface InstallKeyRequest {
  password: string
  disable_password_auth: boolean
  // Sent only on a retry when the VPS forced a password change on first login (an
  // expired password). Used once to complete the change; never stored.
  new_password?: string
}

// Centralized query keys so queries and the mutations that invalidate them never
// drift out of sync.
export const serverKeys = {
  all: ['servers'] as const,
  detail: (id: string) => ['servers', id] as const,
}

// List every server owned by the current user (newest first, ordered server side).
export function useServers() {
  return useQuery({
    queryKey: serverKeys.all,
    queryFn: async (): Promise<Server[]> => {
      const { data } = await apiClient.get<Server[]>('/servers')
      return data
    },
  })
}

// Fetch a single server. Disabled until an id is provided so it does not fire with
// an undefined id.
export function useServer(id: string | undefined) {
  return useQuery({
    queryKey: id ? serverKeys.detail(id) : serverKeys.all,
    enabled: Boolean(id),
    queryFn: async (): Promise<Server> => {
      const { data } = await apiClient.get<Server>(`/servers/${id}`)
      return data
    },
  })
}

// Step one of the add server flow: probe the host and create a pending row.
// Invalidates the list so the new pending server shows up.
export function useProbeServerMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: ProbeRequest): Promise<ProbeResponse> => {
      const { data } = await apiClient.post<ProbeResponse>('/servers/probe', body)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: serverKeys.all })
    },
  })
}

// Step two: install the app public key and verify it. Invalidates both the list
// and the specific server detail so the verified status is reflected everywhere.
export function useInstallKeyMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: {
      serverId: string
      body: InstallKeyRequest
    }): Promise<Server> => {
      const { data } = await apiClient.post<Server>(
        `/servers/${args.serverId}/install_key`,
        args.body,
      )
      return data
    },
    onSuccess: (server) => {
      qc.invalidateQueries({ queryKey: serverKeys.all })
      qc.invalidateQueries({ queryKey: serverKeys.detail(server.id) })
    },
  })
}

// --- Re-registration (host key changed / VPS rebuilt) ---------------------

// Returned by POST /servers/{id}/reregister/probe. Only the fingerprint is surfaced;
// SSH keys stay internal and are never named in the UI.
export interface ReregisterProbeResponse {
  server_id: string
  fingerprint_sha256: string
}

// The structured error codes the re-registration endpoints return. retryable marks a
// transient reachability failure the client may retry with backoff.
export type ReregistrationErrorCode =
  | 'HOST_KEY_CHANGED_AGAIN'
  | 'AUTH_FAILED'
  | 'PASSWORD_AUTH_UNAVAILABLE'
  | 'CHANGE_INCOMPLETE'
  | 'LOCKED_OUT'
  | 'NETWORK_UNREACHABLE'

export interface ReregistrationError {
  code: ReregistrationErrorCode | null
  message: string
  retryable: boolean
}

// Fallback copy per code, and whether a code is retryable, so the UI stays sensible
// even if the backend detail is missing. The backend sends the same user-facing copy,
// which takes precedence when present.
const REREGISTRATION_ERROR_COPY: Record<
  ReregistrationErrorCode,
  { message: string; retryable: boolean }
> = {
  HOST_KEY_CHANGED_AGAIN: {
    message:
      "The server's identity changed again during setup. Start over and re-check the fingerprint.",
    retryable: false,
  },
  AUTH_FAILED: {
    message:
      'That password did not work. Double-check the password from your provider and try again.',
    retryable: false,
  },
  PASSWORD_AUTH_UNAVAILABLE: {
    message:
      "This server does not accept password logins. Reset the root password from your provider's control panel, then try again.",
    retryable: false,
  },
  CHANGE_INCOMPLETE: {
    message:
      "Your provider required a password reset and it could not be completed automatically. Reset the password from your provider's control panel and try again.",
    retryable: false,
  },
  LOCKED_OUT: {
    message:
      "Abstract could not complete the login. Reset the root password from your provider's control panel and try again.",
    retryable: false,
  },
  NETWORK_UNREACHABLE: {
    message: 'Could not reach the server. Check that it is powered on and try again.',
    retryable: true,
  },
}

// Pull the structured re-registration error (code, plain-language message, retryable)
// from a failed request, falling back to the code's canonical copy or a generic
// message for non-structured/non-axios errors.
export function extractReregistrationError(
  error: unknown,
  fallback = 'Re-registration failed.',
): ReregistrationError {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
      const code = detail.code as ReregistrationErrorCode
      const canonical = REREGISTRATION_ERROR_COPY[code]
      return {
        code,
        message:
          typeof detail.message === 'string'
            ? detail.message
            : (canonical?.message ?? fallback),
        retryable:
          typeof detail.retryable === 'boolean'
            ? detail.retryable
            : (canonical?.retryable ?? false),
      }
    }
  }
  return { code: null, message: extractErrorMessage(error, fallback), retryable: false }
}

// Re-registration step one: capture the rebuilt server's new host key and return the
// new fingerprint for the user to confirm. Does not touch the trusted key yet.
export function useReregisterProbeMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (serverId: string): Promise<ReregisterProbeResponse> => {
      const { data } = await apiClient.post<ReregisterProbeResponse>(
        `/servers/${serverId}/reregister/probe`,
      )
      return data
    },
    onSuccess: (_data, serverId) => {
      qc.invalidateQueries({ queryKey: serverKeys.detail(serverId) })
      qc.invalidateQueries({ queryKey: serverKeys.all })
    },
  })
}

// Re-registration step two: complete with the user's root password. The backend runs
// the whole access engine and returns the server verified-but-unhardened. It also
// purges the server's projects (a rebuilt box is a blank slate), so we invalidate the
// project queries too, alongside the server list and detail.
export function useReregisterCompleteMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: {
      serverId: string
      password: string
    }): Promise<Server> => {
      const { data } = await apiClient.post<Server>(
        `/servers/${args.serverId}/reregister/complete`,
        { password: args.password },
      )
      return data
    },
    onSuccess: (server) => {
      qc.invalidateQueries({ queryKey: serverKeys.all })
      qc.invalidateQueries({ queryKey: serverKeys.detail(server.id) })
      qc.invalidateQueries({ queryKey: projectKeys.all })
      qc.invalidateQueries({ queryKey: projectKeys.byServer(server.id) })
    },
  })
}

// Explicitly cancel a still-pending registration. The backend first strips
// Abstract's key off the VPS when it was already installed (a partial install),
// then deletes the row, returning the ordered step list. On a cleanup failure it
// returns a structured 502 and keeps the row (see extractServerDeletionError).
// Invalidates the list and this server's detail.
export function useCancelServerMutation(serverId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<DeleteServerResponse> => {
      const { data } = await apiClient.post<DeleteServerResponse>(
        `/servers/${serverId}/cancel`,
      )
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: serverKeys.all })
      qc.invalidateQueries({ queryKey: serverKeys.detail(serverId) })
    },
  })
}

// Run the hello world smoke test. No cache invalidation: it does not change server
// state, the caller just displays the returned command output.
export function useSmokeTestMutation() {
  return useMutation({
    mutationFn: async (serverId: string): Promise<CommandResult> => {
      const { data } = await apiClient.post<CommandResult>(
        `/servers/${serverId}/smoke_test`,
      )
      return data
    },
  })
}

// --- Hardening ------------------------------------------------------------
//
// Each hardening operation returns the updated Server. They all invalidate the
// detail query (so the page reflects new state) and the list query (so badges on
// the list page update too). A shared factory keeps them consistent.

function useHardeningMutation<TBody = void>(op: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: {
      serverId: string
      body?: TBody
    }): Promise<Server> => {
      const { data } = await apiClient.post<Server>(
        `/servers/${args.serverId}/harden/${op}`,
        args.body ?? {},
      )
      return data
    },
    onSuccess: (server) => {
      qc.invalidateQueries({ queryKey: serverKeys.detail(server.id) })
      qc.invalidateQueries({ queryKey: serverKeys.all })
    },
  })
}

export interface SudoUserBody {
  sudo_user_name: string
}

export const useUpdateSystemMutation = () => useHardeningMutation('update_system')
export const useInstallBasePackagesMutation = () =>
  useHardeningMutation('install_base_packages')
export const useInstallDockerMutation = () => useHardeningMutation('install_docker')
export const useInstallNginxMutation = () => useHardeningMutation('install_nginx')
export const useCreateSudoUserMutation = () =>
  useHardeningMutation<SudoUserBody>('create_sudo_user')
export const useDisableRootLoginMutation = () =>
  useHardeningMutation('disable_root_login')
export const useDisablePasswordAuthMutation = () =>
  useHardeningMutation('disable_password_auth')
export const useConfigureFirewallMutation = () =>
  useHardeningMutation('configure_firewall')
export const useCreateSwapMutation = () => useHardeningMutation('create_swap')
export const useRebootMutation = () => useHardeningMutation('reboot')
export const useQuickHardenMutation = () =>
  useHardeningMutation<SudoUserBody>('quick_harden')

// One-shot ping (used for a manual retry after a reboot timeout).
export function usePingServerMutation() {
  return useMutation({
    mutationFn: async (serverId: string): Promise<{ status: string }> => {
      const { data } = await apiClient.post<{ status: string }>(
        `/servers/${serverId}/ping`,
      )
      return data
    },
  })
}

// --- Server deletion ------------------------------------------------------

// Mirrors the backend ServerDeletionStepResult. project_id/project_name are set
// only for steps that ran inside the per-project deletion loop.
export interface ServerDeletionStepResult {
  name: string
  status: 'completed' | 'skipped' | 'failed'
  detail: string | null
  project_id: string | null
  project_name: string | null
}

export interface DeleteServerResponse {
  success: boolean
  steps: ServerDeletionStepResult[]
}

// One project that a server deletion would destroy, for the confirm dialog.
export interface ServerDeletionPreviewProject {
  id: string
  name: string
  slug: string
  domain: string | null
  runtime_status: 'never_started' | 'running' | 'failed'
}

export interface ServerDeletionPreviewResponse {
  projects: ServerDeletionPreviewProject[]
}

// Read-only preview of the projects a deletion would remove. Enabled only while
// the dialog is open so it does not fetch in the background.
export function useServerDeletionPreview(
  serverId: string | undefined,
  enabled: boolean,
) {
  return useQuery({
    queryKey: serverId
      ? [...serverKeys.detail(serverId), 'deletion_preview']
      : serverKeys.all,
    enabled: enabled && Boolean(serverId),
    queryFn: async (): Promise<ServerDeletionPreviewResponse> => {
      const { data } = await apiClient.get<ServerDeletionPreviewResponse>(
        `/servers/${serverId}/deletion_preview`,
      )
      return data
    },
  })
}

// Delete a server and everything Abstract put on it. Invalidates the server list,
// this server's detail, and the project list (its projects are gone). The caller
// handles navigation away from the now-deleted server on success.
export function useDeleteServerMutation(serverId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<DeleteServerResponse> => {
      const { data } = await apiClient.delete<DeleteServerResponse>(
        `/servers/${serverId}`,
      )
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: serverKeys.all })
      qc.invalidateQueries({ queryKey: serverKeys.detail(serverId) })
      qc.invalidateQueries({ queryKey: projectKeys.all })
    },
  })
}

// Pull the structured 502 body a failed server deletion returns (message, failed
// step, failed project, and the ordered step list) so the dialog can show exactly
// where it stopped. Falls back to extractErrorMessage for plain details (e.g. a
// 409 while an operation is already in flight) and non-axios errors.
export function extractServerDeletionError(
  error: unknown,
  fallback = 'Deleting the server failed',
): {
  message: string
  failedStep: string | null
  failedProjectName: string | null
  steps: ServerDeletionStepResult[]
} {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (detail && typeof detail === 'object') {
      return {
        message: typeof detail.message === 'string' ? detail.message : fallback,
        failedStep:
          typeof detail.failed_step === 'string' ? detail.failed_step : null,
        failedProjectName:
          typeof detail.failed_project_name === 'string'
            ? detail.failed_project_name
            : null,
        steps: Array.isArray(detail.steps) ? detail.steps : [],
      }
    }
  }
  return {
    message: extractErrorMessage(error, fallback),
    failedStep: null,
    failedProjectName: null,
    steps: [],
  }
}
