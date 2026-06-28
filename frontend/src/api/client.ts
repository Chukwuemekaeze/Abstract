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
