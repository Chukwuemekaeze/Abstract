// TanStack Query hooks for the per-project env files API.
//
// The backend never returns env var values after they are saved: list items
// carry counts, details carry keys only. Values exist client side only while
// the user is typing them in the dialog.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiClient } from '@/api/client'

export interface EnvFileListItem {
  id: string
  path: string
  variable_count: number
  updated_at: string
}

export interface EnvFileDetail {
  id: string
  path: string
  keys: string[]
  updated_at: string
}

export interface CreateEnvFileRequest {
  path: string
  variables: Record<string, string>
}

// Partial update: set_variables upserts, remove_keys deletes; keys not
// mentioned keep their stored values.
export interface UpdateEnvFileRequest {
  path?: string
  set_variables?: Record<string, string>
  remove_keys?: string[]
}

export const envFileKeys = {
  list: (projectId: string) => ['projects', projectId, 'env-files'] as const,
  detail: (projectId: string, envFileId: string) =>
    ['projects', projectId, 'env-files', envFileId] as const,
}

export function useEnvFiles(projectId: string) {
  return useQuery({
    queryKey: envFileKeys.list(projectId),
    queryFn: async (): Promise<EnvFileListItem[]> => {
      const { data } = await apiClient.get<EnvFileListItem[]>(
        `/projects/${projectId}/env-files`,
      )
      return data
    },
  })
}

// Keys only; fetched when the edit dialog opens.
export function useEnvFile(projectId: string, envFileId: string | null) {
  return useQuery({
    queryKey: envFileKeys.detail(projectId, envFileId ?? 'none'),
    enabled: Boolean(envFileId),
    queryFn: async (): Promise<EnvFileDetail> => {
      const { data } = await apiClient.get<EnvFileDetail>(
        `/projects/${projectId}/env-files/${envFileId}`,
      )
      return data
    },
  })
}

// True when the user manages a root .env themselves; drives the "a root .env
// will be auto-generated" hint in the UI.
export function useHasRootEnvFile(projectId: string): boolean {
  const { data } = useEnvFiles(projectId)
  return (data ?? []).some((file) => file.path === '.env')
}

function useInvalidateEnvFiles(projectId: string) {
  const qc = useQueryClient()
  return () =>
    qc.invalidateQueries({ queryKey: envFileKeys.list(projectId) })
}

export function useCreateEnvFileMutation(projectId: string) {
  const invalidate = useInvalidateEnvFiles(projectId)
  return useMutation({
    mutationFn: async (body: CreateEnvFileRequest): Promise<EnvFileDetail> => {
      const { data } = await apiClient.post<EnvFileDetail>(
        `/projects/${projectId}/env-files`,
        body,
      )
      return data
    },
    onSuccess: invalidate,
  })
}

export function useUpdateEnvFileMutation(projectId: string, envFileId: string) {
  const invalidate = useInvalidateEnvFiles(projectId)
  return useMutation({
    mutationFn: async (body: UpdateEnvFileRequest): Promise<EnvFileDetail> => {
      const { data } = await apiClient.patch<EnvFileDetail>(
        `/projects/${projectId}/env-files/${envFileId}`,
        body,
      )
      return data
    },
    onSuccess: invalidate,
  })
}

export function useDeleteEnvFileMutation(projectId: string) {
  const invalidate = useInvalidateEnvFiles(projectId)
  return useMutation({
    mutationFn: async (envFileId: string): Promise<void> => {
      await apiClient.delete(`/projects/${projectId}/env-files/${envFileId}`)
    },
    onSuccess: invalidate,
  })
}
