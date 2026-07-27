import { describe, expect, it } from 'vitest'

import { extractErrorMessage } from '@/api/client'

// Shape enough of an axios error for axios.isAxiosError to accept it.
function axiosError(detail: unknown, message = '') {
  return { isAxiosError: true, message, response: { data: { detail } } }
}

describe('extractErrorMessage', () => {
  it('returns the backend detail string', () => {
    expect(extractErrorMessage(axiosError('Server exploded'), 'fallback')).toBe(
      'Server exploded',
    )
  })

  it('falls back instead of returning a blank message when detail is empty', () => {
    // An unreachable server can surface a 502 whose exception string is empty; the
    // toast must never render blank.
    expect(extractErrorMessage(axiosError(''), 'Smoke test failed.')).toBe(
      'Smoke test failed.',
    )
    expect(extractErrorMessage(axiosError('   '), 'Smoke test failed.')).toBe(
      'Smoke test failed.',
    )
  })

  it('prefers the axios message over the fallback when detail is blank', () => {
    expect(extractErrorMessage(axiosError('', 'Network Error'), 'fallback')).toBe(
      'Network Error',
    )
  })
})
