// TanStack Query hooks for the projects API.
//
// Same conventions as api/servers.ts: stable query keys, mutations invalidate
// the keys they affect, all calls go through the shared axios client.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiClient } from '@/api/client'

// Shape returned by the backend ProjectResponse schema. Key material and the
// GitHub deploy key id are deliberately never sent by the backend, so they are
// absent here too. server_name and server_host are only present on the
// top-level list endpoint (ProjectListItemResponse).
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
  server_name?: string
  server_host?: string
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
