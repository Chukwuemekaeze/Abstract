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

// True when install_key reported that the VPS forces a password change on first login
// (an expired password). The backend returns a 409 with a structured detail carrying
// { code: 'password_change_required' } so the dialog can reveal a new-password field.
export function isPasswordChangeRequired(error: unknown): boolean {
  if (axios.isAxiosError(error) && error.response?.status === 409) {
    const detail = error.response.data?.detail
    return (
      !!detail &&
      typeof detail === 'object' &&
      detail.code === 'password_change_required'
    )
  }
  return false
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
