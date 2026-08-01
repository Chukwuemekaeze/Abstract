import { act } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AxiosError } from 'axios'

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

  it('surfaces the structured auth error message when the root password is wrong', async () => {
    const user = userEvent.setup()
    // install_key returns a structured 400 { code, message } for a rejected password.
    const err = new AxiosError('Request failed with status code 400', 'ERR_BAD_REQUEST')
    err.response = {
      data: {
        detail: {
          code: 'AUTH_FAILED',
          message:
            'That password did not work. Double-check the root password for this server and try again.',
          retryable: false,
        },
      },
    } as never
    post.mockRejectedValueOnce(err)

    renderWithProviders(<AddServerDialog />)
    resumeIntoConfirmation()

    await user.type(screen.getByLabelText('Password'), 'wrongpass')
    await user.click(
      screen.getByRole('button', { name: /fingerprint matches, install key/i }),
    )

    // The plain-language message is shown, not the generic "status code 400".
    expect(
      await screen.findByText(/That password did not work/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/status code 400/)).not.toBeInTheDocument()
    expect(useAddServerStore.getState().step).toBe('failed')
  })

  it('installs in a single call without collecting a new password (forced changes are handled server-side)', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({ data: { id: 'srv-1', status: 'verified' } })

    renderWithProviders(<AddServerDialog />)
    resumeIntoConfirmation()

    await user.type(screen.getByLabelText('Password'), 'expired-pw')
    await user.click(
      screen.getByRole('button', { name: /fingerprint matches, install key/i }),
    )

    // The dialog reaches the done step, and no new-password field is ever shown.
    await waitFor(() =>
      expect(useAddServerStore.getState().step).toBe('done'),
    )
    expect(screen.queryByLabelText('New root password')).not.toBeInTheDocument()

    // Exactly one install call, carrying only the password and hardening flag.
    expect(post).toHaveBeenCalledTimes(1)
    const [, body] = post.mock.calls[0]
    expect(body).toEqual({ password: 'expired-pw', disable_password_auth: true })
    expect(body).not.toHaveProperty('new_password')
  })
})
