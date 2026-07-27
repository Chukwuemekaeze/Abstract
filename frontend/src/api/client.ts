import axios from 'axios'

import { getAuthToken } from '@/lib/auth-token'

// Single axios instance. baseURL '/api' is proxied to the FastAPI backend in dev
// (see vite.config.ts). Auth is token based: every request carries the Clerk
// session token in the Authorization header.
export const apiClient = axios.create({
  baseURL: '/api',
})

// Attach the Clerk session token to every outgoing request.
apiClient.interceptors.request.use(async (config) => {
  const token = await getAuthToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// On 401 the session is gone or invalid: bounce to sign in.
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      window.location.href = '/sign-in'
    }
    return Promise.reject(error)
  },
)

// Pull a human readable message out of an axios error, falling back sensibly.
export function extractErrorMessage(error: unknown, fallback = 'Something went wrong'): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (error.message) return error.message
  }
  if (error instanceof Error) return error.message
  return fallback
}

// A clean, user-facing message for a teardown that failed at the very first VPS
// step, 'connect_ssh': Abstract never reached the box (it is powered off, firewalled,
// or otherwise unreachable). Returned in place of the raw backend "failed at step
// 'connect_ssh'" text so the top-line message reads plainly; the technical per-step
// detail is still shown in the step list below it. Returns null for any other step so
// the caller keeps the backend's own message.
export function connectionFailureMessage(failedStep: string | null): string | null {
  if (failedStep !== 'connect_ssh') return null
  return (
    "Abstract couldn't reach the server. It may be powered off or unreachable — " +
    "make sure it's online, then retry."
  )
}

// Pull a hardening failure out of an axios error. Hardening endpoints return a
// structured detail object { message, captured_output } on a 502 so the UI can show
// the raw shell output in a collapsible panel. Falls back to extractErrorMessage for
// plain string details (guardrail 400s, 409s) and non-axios errors.
export function extractHardeningError(
  error: unknown,
  fallback = 'Operation failed',
): { message: string; output: string | null } {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (detail && typeof detail === 'object') {
      return {
        message: typeof detail.message === 'string' ? detail.message : fallback,
        output:
          typeof detail.captured_output === 'string'
            ? detail.captured_output
            : null,
      }
    }
  }
  return { message: extractErrorMessage(error, fallback), output: null }
}
