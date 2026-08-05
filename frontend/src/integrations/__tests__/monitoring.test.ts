// Mirrors the backend's external-logging/monitoring gating tests: monitoring is
// inert until configured. With no DSN, initMonitoring must not touch the SDK, and
// the helpers must stay safe to call — so dev and test need no special casing.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const initMock = vi.fn()
const setUserMock = vi.fn()
const captureExceptionMock = vi.fn()

vi.mock('@sentry/react', () => ({
  init: (...args: unknown[]) => initMock(...args),
  setUser: (...args: unknown[]) => setUserMock(...args),
  captureException: (...args: unknown[]) => captureExceptionMock(...args),
  captureMessage: vi.fn(),
  reactRouterBrowserTracingIntegration: vi.fn(),
  replayIntegration: vi.fn(),
}))

beforeEach(() => {
  vi.clearAllMocks()
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('monitoring', () => {
  it('is a no-op when no DSN is configured', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', '')
    const { initMonitoring } = await import('@/integrations/monitoring')

    initMonitoring()

    expect(initMock).not.toHaveBeenCalled()
  })

  it('initializes Sentry with the DSN when configured', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', 'https://public@example.ingest.sentry.io/1')
    const { initMonitoring } = await import('@/integrations/monitoring')

    initMonitoring()

    expect(initMock).toHaveBeenCalledOnce()
    expect(initMock.mock.calls[0][0]).toMatchObject({
      dsn: 'https://public@example.ingest.sentry.io/1',
    })
  })

  it('helpers stay safe to call when uninitialized', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', '')
    const { setUser, clearUser, captureException } = await import('@/integrations/monitoring')

    expect(() => setUser('user-123')).not.toThrow()
    expect(() => clearUser()).not.toThrow()
    expect(() => captureException(new Error('boom'))).not.toThrow()
    expect(setUserMock).toHaveBeenCalledWith({ id: 'user-123' })
  })
})
