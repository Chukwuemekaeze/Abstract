import { act } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Mock the axios client so the mutations never hit the network. extractErrorMessage
// is kept as a simple passthrough to the fallback.
vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

import { apiClient } from '@/api/client'
import { AddServerDialog } from '@/components/AddServerDialog'
import { useAddServerStore } from '@/store/addServerStore'
import { renderWithProviders } from '@/test/utils'

const post = apiClient.post as unknown as Mock
const get = apiClient.get as unknown as Mock

const resumableServer = {
  id: 'srv-1',
  name: 'pending-web',
  host: '203.0.113.50',
  port: 22,
  username: 'root',
  fingerprint_sha256: 'SHA256:abcdef0123456789',
}

function resumeIntoConfirmation() {
  act(() => {
    useAddServerStore.getState().resume(resumableServer)
  })
}

describe('AddServerDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useAddServerStore.getState().close()
  })

  it('resumes at the fingerprint step with an empty password and install gated on it', async () => {
    const user = userEvent.setup()
    renderWithProviders(<AddServerDialog />)
    resumeIntoConfirmation()

    // The captured fingerprint is shown for the TOFU comparison.
    expect(await screen.findByText(/SHA256:abcdef0123456789/)).toBeInTheDocument()

    // Password starts empty (never persisted), and install is disabled until entered.
    const password = screen.getByLabelText('Password') as HTMLInputElement
    expect(password.value).toBe('')
    const install = screen.getByRole('button', {
      name: /fingerprint matches, install key/i,
    })
    expect(install).toBeDisabled()

    await user.type(password, 'hunter2')
    expect(install).toBeEnabled()
  })

  it('does not delete the pending registration when the dialog is dismissed with Escape', async () => {
    const user = userEvent.setup()
    renderWithProviders(<AddServerDialog />)
    resumeIntoConfirmation()
    expect(await screen.findByText(/SHA256:abcdef0123456789/)).toBeInTheDocument()

    await user.keyboard('{Escape}')

    // Dialog closed locally...
    await waitFor(() =>
      expect(screen.queryByText(/SHA256:abcdef0123456789/)).not.toBeInTheDocument(),
    )
    expect(useAddServerStore.getState().step).toBe('idle')
    // ...but the backend was never asked to cancel/delete anything.
    expect(post).not.toHaveBeenCalled()
    expect(get).not.toHaveBeenCalled()
  })

  it('shows the failed step (and does not cancel the pending row) when install fails', async () => {
    const user = userEvent.setup()
    post.mockRejectedValueOnce(new Error('boom'))

    renderWithProviders(<AddServerDialog />)
    resumeIntoConfirmation()

    await user.type(screen.getByLabelText('Password'), 'hunter2')
    await user.click(
      screen.getByRole('button', { name: /fingerprint matches, install key/i }),
    )

    // The failed step renders with a retry, and the store reflects it.
    expect(await screen.findByText('Key installation failed.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(useAddServerStore.getState().step).toBe('failed')

    // Exactly one call — the install attempt. No cancel/delete of the pending row.
    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith(
      '/servers/srv-1/install_key',
      expect.objectContaining({ password: 'hunter2' }),
    )
    expect(post.mock.calls.every(([url]) => !String(url).includes('/cancel'))).toBe(
      true,
    )
  })
})
