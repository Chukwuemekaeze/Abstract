import axios from 'axios'

// Single axios instance. baseURL '/api' is proxied to the FastAPI backend in dev
// (see vite.config.ts). withCredentials is on so cookies work once real auth lands.
export const apiClient = axios.create({
  baseURL: '/api',
  withCredentials: true,
})

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
