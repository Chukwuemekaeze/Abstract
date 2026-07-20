import { act } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Mock the axios client so the mutations never hit the network. extractErrorMessage
// is kept as a simple passthrough to the fallback.
vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
  // Mirror the real helper's shape check so the tests can drive the branch.
  isPasswordChangeRequired: (err: { response?: { data?: { detail?: { code?: string } } } }) =>
    err?.response?.data?.detail?.code === 'password_change_required',
}))

// Toasts are noise here.
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import { apiClient } from '@/api/client'
import { ReRegisterServerDialog } from '@/components/servers/ReRegisterServerDialog'
import { useReRegisterServerDialogStore } from '@/store/reregister-server-dialog'
import { makePendingServer, renderWithProviders } from '@/test/utils'

const post = apiClient.post as unknown as Mock
const get = apiClient.get as unknown as Mock

function openDialog() {
  act(() => {
    useReRegisterServerDialogStore.getState().openWith('srv-1')
  })
}

describe('ReRegisterServerDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useReRegisterServerDialogStore.getState().close()
    // The dialog reads the server for its title.
    get.mockResolvedValue({
      data: makePendingServer({ id: 'srv-1', name: 'web1', status: 'key_mismatch' }),
    })
  })

  it('warns that re-registering wipes the stale project/deployment state', async () => {
    renderWithProviders(<ReRegisterServerDialog />)
    openDialog()

    expect(
      await screen.findByText(/Abstract's record of the projects and deployments/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/Hardening state .* is reset/i)).toBeInTheDocument()
    // Username defaults to root and is editable.
    expect((screen.getByLabelText('Username') as HTMLInputElement).value).toBe('root')
  })

  it('re-probes, shows the new fingerprint, and gates install on the password', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({
      data: {
        server_id: 'srv-1',
        fingerprint_sha256: 'SHA256:newfingerprint999',
        app_public_key: 'ssh-ed25519 AAAANEW abstract-new',
      },
    })
    post.mockResolvedValueOnce({
      data: makePendingServer({ id: 'srv-1', status: 'verified' }),
    })

    renderWithProviders(<ReRegisterServerDialog />)
    openDialog()

    // Step one: re-probe the current host.
    await user.click(await screen.findByRole('button', { name: /continue/i }))

    // The NEW fingerprint is shown for out-of-band confirmation.
    expect(await screen.findByText(/SHA256:newfingerprint999/)).toBeInTheDocument()
    expect(post).toHaveBeenCalledWith('/servers/srv-1/reprobe', { username: 'root' })

    // Install is gated on the password.
    const install = screen.getByRole('button', {
      name: /fingerprint matches, install key/i,
    })
    expect(install).toBeDisabled()

    await user.type(screen.getByLabelText('Password'), 'hunter2')
    expect(install).toBeEnabled()

    // Step two reuses install_key with the confirmed password.
    await user.click(install)
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        '/servers/srv-1/install_key',
        expect.objectContaining({ password: 'hunter2', disable_password_auth: true }),
      ),
    )
    expect(await screen.findByText(/Server re-registered/i)).toBeInTheDocument()
  })

  it('reveals the new-password field when the VPS forces a password change', async () => {
    const user = userEvent.setup()
    // Re-probe succeeds.
    post.mockResolvedValueOnce({
      data: {
        server_id: 'srv-1',
        fingerprint_sha256: 'SHA256:newfingerprint999',
        app_public_key: 'ssh-ed25519 AAAANEW abstract-new',
      },
    })
    // First install: server forces a password change (expired password).
    post.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'password_change_required' } } },
    })
    // Retry with the new password succeeds.
    post.mockResolvedValueOnce({
      data: makePendingServer({ id: 'srv-1', status: 'verified' }),
    })

    renderWithProviders(<ReRegisterServerDialog />)
    openDialog()

    await user.click(await screen.findByRole('button', { name: /continue/i }))
    await user.type(await screen.findByLabelText('Password'), 'expired-pw')
    await user.click(
      screen.getByRole('button', { name: /fingerprint matches, install key/i }),
    )

    // The new-password field appears; install is gated on it.
    const newPassword = await screen.findByLabelText('New root password')
    const install = screen.getByRole('button', {
      name: /fingerprint matches, install key/i,
    })
    expect(install).toBeDisabled()
    await user.type(newPassword, 'a-fresh-strong-password')
    expect(install).toBeEnabled()

    await user.click(install)
    await waitFor(() =>
      expect(post).toHaveBeenLastCalledWith(
        '/servers/srv-1/install_key',
        expect.objectContaining({
          password: 'expired-pw',
          new_password: 'a-fresh-strong-password',
        }),
      ),
    )
  })

  it('shows a retry on a failed re-probe without calling install', async () => {
    const user = userEvent.setup()
    post.mockRejectedValueOnce(new Error('boom'))

    renderWithProviders(<ReRegisterServerDialog />)
    openDialog()

    await user.click(await screen.findByRole('button', { name: /continue/i }))

    expect(await screen.findByText('Could not reach the host.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    // Only the reprobe was attempted; install was never called.
    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith('/servers/srv-1/reprobe', { username: 'root' })
  })
})
