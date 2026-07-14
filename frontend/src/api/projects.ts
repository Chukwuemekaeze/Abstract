// TanStack Query hooks for the projects API.
//
// Same conventions as api/servers.ts: stable query keys, mutations invalidate
// the keys they affect, all calls go through the shared axios client.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'

import { apiClient, extractErrorMessage } from '@/api/client'

// Shape returned by the backend ProjectResponse schema. Key material and the
// GitHub deploy key id are deliberately never sent by the backend, so they are
// absent here too. server_name and server_host are only present on the
// top-level list endpoint (ProjectListItemResponse).
export type RuntimeStatus = 'never_started' | 'running' | 'failed'

export interface Project {
  id: string
  name: string
  slug: string
  server_id: string
  github_repo_full_name: string
  github_repo_id: number
  clone_path: string
  cloned_at: string | null
  created_at: string
  updated_at: string
  deploy_key_fingerprint: string
  runtime_status: RuntimeStatus
  started_at: string | null
  compose_file_path: string | null
  domain: string | null
  internal_port: number | null
  published_at: string | null
  is_deleting: boolean
  server_name?: string
  server_host?: string
}

// One step of a project deletion, mirroring the backend DeletionStepResult.
export interface DeletionStepResult {
  name: string
  status: 'completed' | 'skipped' | 'failed'
  detail: string | null
}

export interface DeleteProjectResponse {
  success: boolean
  steps: DeletionStepResult[]
}

export interface RunResult {
  runtime_status: RuntimeStatus
  started_at: string | null
  captured_output: string | null
  build_output?: string | null
}

export interface DetectedPort {
  service: string
  host_port: number
  container_port: number
  is_dangerous: boolean
}

export interface PublishRequest {
  domain: string
  internal_port: number
}

export interface PullResult {
  before_commit: string
  after_commit: string
  already_up_to_date: boolean
  updated_at: string
}

// Returned by GET /github/repos, already sorted by pushed_at descending.
export interface GithubRepo {
  id: number
  full_name: string
  name: string
  pushed_at: string
  private: boolean
}

export interface CreateProjectRequest {
  name: string
  server_id: string
  github_repo_id: number
  github_repo_full_name: string
}

export const projectKeys = {
  all: ['projects'] as const,
  byServer: (serverId: string) => ['projects', 'server', serverId] as const,
  githubRepos: ['github', 'repos'] as const,
  detectedPorts: (projectId: string) =>
    ['projects', projectId, 'detected-ports'] as const,
}

// All of the user's projects across all servers, newest first, with server
// name and host attached server side.
export function useProjects() {
  return useQuery({
    queryKey: projectKeys.all,
    queryFn: async (): Promise<Project[]> => {
      const { data } = await apiClient.get<Project[]>('/projects')
      return data
    },
  })
}

// Projects on one server, newest first.
export function useProjectsByServer(serverId: string | undefined) {
  return useQuery({
    queryKey: serverId ? projectKeys.byServer(serverId) : projectKeys.all,
    enabled: Boolean(serverId),
    queryFn: async (): Promise<Project[]> => {
      const { data } = await apiClient.get<Project[]>(
        `/servers/${serverId}/projects`,
      )
      return data
    },
  })
}

// The user's GitHub repos with admin permission, newest push first. Cached for
// a minute so reopening the dialog does not double-hit GitHub.
export function useGithubRepos() {
  return useQuery({
    queryKey: projectKeys.githubRepos,
    staleTime: 60_000,
    queryFn: async (): Promise<GithubRepo[]> => {
      const { data } = await apiClient.get<GithubRepo[]>('/github/repos')
      return data
    },
  })
}

// Create a project (deploy key + VPS setup + clone). Slow by nature: the
// backend clones the repo before responding. Invalidates both the global list
// and the owning server's list.
export function useCreateProjectMutation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: CreateProjectRequest): Promise<Project> => {
      const { data } = await apiClient.post<Project>('/projects', body)
      return data
    },
    onSuccess: (project) => {
      qc.invalidateQueries({ queryKey: projectKeys.all })
      qc.invalidateQueries({ queryKey: projectKeys.byServer(project.server_id) })
    },
  })
}

// Invalidate every projects list so runtime/publish state updates wherever the
// card is rendered (global list and server detail page).
function useInvalidateProjects() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: projectKeys.all })
}

// Start (or restart) the app: the backend writes env files to the VPS and runs
// `docker compose up -d --build`. Slow by nature; a first build can take
// minutes. Invalidates on error too, because a failed start still flips
// runtime_status to failed.
export function useStartProjectMutation(projectId: string) {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: async (): Promise<RunResult> => {
      const { data } = await apiClient.post<RunResult>(
        `/projects/${projectId}/start`,
      )
      return data
    },
    onSettled: invalidate,
  })
}

// Sync the clone with origin's default branch (fetch + hard reset on the
// VPS). onSuccess (not onSettled): a failed pull changes no DB state.
export function usePullProjectMutation(projectId: string) {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: async (): Promise<PullResult> => {
      const { data } = await apiClient.post<PullResult>(
        `/projects/${projectId}/pull`,
      )
      return data
    },
    onSuccess: invalidate,
  })
}

// Re-derive runtime_status from `docker compose ps` on the server.
export function useRefreshStatusMutation(projectId: string) {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: async (): Promise<Project> => {
      const { data } = await apiClient.post<Project>(
        `/projects/${projectId}/refresh_status`,
      )
      return data
    },
    onSuccess: invalidate,
  })
}

// Host-published ports of the running containers. Only fetched while the
// publish dialog is open (enabled flag), never on card render.
export function useDetectedPorts(
  projectId: string,
  options: { enabled: boolean },
) {
  return useQuery({
    queryKey: projectKeys.detectedPorts(projectId),
    enabled: options.enabled,
    staleTime: 0,
    queryFn: async (): Promise<DetectedPort[]> => {
      const { data } = await apiClient.get<DetectedPort[]>(
        `/projects/${projectId}/detected_ports`,
      )
      return data
    },
  })
}

// Publish the running app to a domain via nginx + Let's Encrypt. Slow: the
// backend waits on certbot and an HTTPS verification round trip.
export function usePublishProjectMutation(projectId: string) {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: async (body: PublishRequest): Promise<Project> => {
      const { data } = await apiClient.post<Project>(
        `/projects/${projectId}/publish`,
        body,
      )
      return data
    },
    onSuccess: invalidate,
  })
}

// Delete a project: reverse of create plus post-create cleanup (unpublish,
// stop containers, remove the clone, revoke the deploy key, drop the row).
// Invalidates the global list and the owning server's list on success.
export function useDeleteProjectMutation(projectId: string, serverId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<DeleteProjectResponse> => {
      const { data } = await apiClient.delete<DeleteProjectResponse>(
        `/projects/${projectId}`,
      )
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: projectKeys.all })
      qc.invalidateQueries({ queryKey: projectKeys.byServer(serverId) })
    },
  })
}

// Pull the structured deletion failure out of a 502. The backend returns
// { message, failed_step, steps } so the dialog can show which step failed and
// the full per-step progress. Falls back to extractErrorMessage for plain
// details (e.g. a 409 while a delete is already in flight) and non-axios errors.
export function extractDeletionError(
  error: unknown,
  fallback = 'Deleting the project failed',
): { message: string; failedStep: string | null; steps: DeletionStepResult[] } {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (detail && typeof detail === 'object') {
      return {
        message: typeof detail.message === 'string' ? detail.message : fallback,
        failedStep:
          typeof detail.failed_step === 'string' ? detail.failed_step : null,
        steps: Array.isArray(detail.steps) ? detail.steps : [],
      }
    }
  }
  return {
    message: extractErrorMessage(error, fallback),
    failedStep: null,
    steps: [],
  }
}

// Advanced settings; v1 carries only compose_file_path (null clears it).
export function useUpdateProjectMutation(projectId: string) {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: async (body: {
      compose_file_path: string | null
    }): Promise<Project> => {
      const { data } = await apiClient.patch<Project>(
        `/projects/${projectId}`,
        body,
      )
      return data
    },
    onSuccess: invalidate,
  })
}
