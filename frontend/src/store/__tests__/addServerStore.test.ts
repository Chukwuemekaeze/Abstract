import { beforeEach, describe, expect, it } from 'vitest'

import { useAddServerStore } from '@/store/addServerStore'

const sampleServer = {
  id: 'srv-42',
  name: 'pending-web',
  host: '203.0.113.50',
  port: 2222,
  username: 'deploy',
  fingerprint_sha256: 'SHA256:zzz',
}

describe('addServerStore', () => {
  beforeEach(() => {
    useAddServerStore.getState().close()
  })

  it('resume() jumps to fingerprint confirmation with details prefilled and a blank password', () => {
    useAddServerStore.getState().resume(sampleServer)
    const state = useAddServerStore.getState()

    expect(state.step).toBe('awaiting_confirmation')
    expect(state.formData.name).toBe('pending-web')
    expect(state.formData.host).toBe('203.0.113.50')
    expect(state.formData.port).toBe(2222)
    expect(state.formData.username).toBe('deploy')
    // The password is never persisted, so it must be re-entered on resume.
    expect(state.formData.password).toBe('')
    expect(state.pendingServer?.id).toBe('srv-42')
    expect(state.pendingServer?.fingerprint).toBe('SHA256:zzz')
  })

  it('close() fully resets, including any entered password', () => {
    useAddServerStore.getState().resume(sampleServer)
    useAddServerStore.getState().setFormData({ password: 'secret' })

    useAddServerStore.getState().close()
    const state = useAddServerStore.getState()

    expect(state.step).toBe('idle')
    expect(state.pendingServer).toBeNull()
    expect(state.formData.password).toBe('')
    expect(state.formData.name).toBe('')
  })

  it('resume() tolerates a missing fingerprint', () => {
    useAddServerStore.getState().resume({ ...sampleServer, fingerprint_sha256: null })
    expect(useAddServerStore.getState().pendingServer?.fingerprint).toBe('')
  })
})
