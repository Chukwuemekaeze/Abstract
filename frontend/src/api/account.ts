// Account-level API. Backend-driven account deletion: the user deletes against our
// backend, which tears down every server, purges our DB row, then deletes the Clerk
// user. The caller signs the user out on success (their Clerk session is gone).

import { useMutation, useQuery } from '@tanstack/react-query'
import axios from 'axios'

import { apiClient, extractErrorMessage } from '@/api/client'

export interface DeleteAccountResponse {
  success: boolean
}

// The signed-in user's own identity. `id` is the Postgres user id — the same key
// the backend tags its logs and errors with — so the frontend can attribute its
// monitoring events to it and correlate the two sides.
export interface CurrentUser {
  id: string
  email: string
}

// Fetch the signed-in user's identity. Enabled by the caller once Clerk reports
// the user is signed in (the axios client attaches the Clerk token). Cached
// effectively forever within a session: the id never changes while signed in.
export function useCurrentUserQuery(enabled: boolean) {
  return useQuery({
    queryKey: ['currentUser'],
    queryFn: async (): Promise<CurrentUser> => {
      const { data } = await apiClient.get<CurrentUser>('/account/me')
      return data
    },
    enabled,
    staleTime: Infinity,
  })
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

// One entry of the backend's ordered teardown step list. On a 409 the failed step
// tells us *why* a server blocked deletion (e.g. name 'connect_ssh' = unreachable),
// which we turn into a clean, user-facing sentence rather than echoing the raw
// backend message that names internal steps.
interface DeletionStep {
  name: string
  status: string
}

interface AccountDeletionDetail {
  message?: string
  blocking_server_name?: string
  steps?: DeletionStep[]
}

// Build a clean, human message for a server that blocked account deletion, from the
// structured 409 detail. Returns null when there is no failed step to explain (for
// example a server that was simply busy), so the caller falls back to the backend's
// own — already clean — message for that case.
function blockingServerMessage(detail: AccountDeletionDetail): string | null {
  const name = detail.blocking_server_name
  if (!name) return null
  const failed = detail.steps?.find((s) => s.status === 'failed')
  if (!failed) return null

  // 'connect_ssh' is the very first VPS step: we never reached the box at all.
  if (failed.name === 'connect_ssh') {
    return (
      `Server '${name}' couldn't be reached, so it wasn't torn down. Make sure ` +
      `it's powered on and online, then try again — or delete that server first.`
    )
  }
  // The box answered but a later teardown step failed. Point the user at the same
  // resolve-or-delete escape hatch without naming the internal step.
  return (
    `Server '${name}' couldn't be fully torn down. Resolve or delete that ` +
    `server, then try again.`
  )
}

// Pull a clean message out of a failed account deletion. A 409 carries a structured
// detail { message, blocking_server_name, steps } naming the server that must be
// resolved first; we rebuild the sentence from the failed step so no internal step
// name leaks to the user. A 502 carries { message } when the local data was purged
// but the Clerk delete failed. Falls back to extractErrorMessage for anything else.
export function extractAccountDeletionError(
  error: unknown,
  fallback = 'Deleting your account failed',
): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (detail && typeof detail === 'object') {
      const clean = blockingServerMessage(detail as AccountDeletionDetail)
      if (clean) return clean
      if (typeof (detail as AccountDeletionDetail).message === 'string') {
        return (detail as AccountDeletionDetail).message as string
      }
    }
  }
  return extractErrorMessage(error, fallback)
}
