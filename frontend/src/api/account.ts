// Account-level API. Backend-driven account deletion: the user deletes against our
// backend, which tears down every server, purges our DB row, then deletes the Clerk
// user. The caller signs the user out on success (their Clerk session is gone).

import { useMutation } from '@tanstack/react-query'
import axios from 'axios'

import { apiClient, extractErrorMessage } from '@/api/client'

export interface DeleteAccountResponse {
  success: boolean
}

// Delete the signed-in user's account. On success everything the user owns is gone,
// so there are no query keys worth invalidating; the caller clears the cache and
// signs out instead.
export function useDeleteAccountMutation() {
  return useMutation({
    mutationFn: async (): Promise<DeleteAccountResponse> => {
      const { data } = await apiClient.delete<DeleteAccountResponse>('/account')
      return data
    },
  })
}

// Pull the message out of a failed account deletion. A 409 carries a structured
// detail { message, blocking_server_name, ... } naming the server that must be
// resolved first; a 502 carries { message } when the local data was purged but the
// Clerk delete failed. Falls back to extractErrorMessage for anything else.
export function extractAccountDeletionError(
  error: unknown,
  fallback = 'Deleting your account failed',
): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
      return detail.message
    }
  }
  return extractErrorMessage(error, fallback)
}
