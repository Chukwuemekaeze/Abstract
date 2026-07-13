// TanStack Query hooks for the servers API.
//
// Queries read data and are cached under stable query keys. Mutations change data
// and invalidate the relevant query keys on success so the UI refetches and stays
// consistent. All network calls go through the shared axios client (baseURL '/api').

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiClient } from '@/api/client'

// Mirrors the backend server status check constraint.
export type ServerStatus = 'pending_verification' | 'verified' | 'key_mismatch'

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

// Abort a still pending server (deletes the row). Invalidates the list.
export function useCancelServerMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (serverId: string): Promise<void> => {
      await apiClient.post(`/servers/${serverId}/cancel`)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: serverKeys.all })
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
