import { act } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Mock the axios client so the mutations and the progress poll never hit the network.
vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

import { apiClient } from '@/api/client'
import { ReregisterDialog } from '@/components/servers/ReregisterDialog'
import { useReregisterDialogStore } from '@/store/reregisterDialogStore'
import { makePendingServer } from '@/test/utils'
import { renderWithProviders } from '@/test/utils'

const post = apiClient.post as unknown as Mock
const get = apiClient.get as unknown as Mock

function openDialog() {
  act(() => {
    useReregisterDialogStore.getState().openWith('srv-1')
  })
}

// An axios-shaped structured error the backend returns for re-registration failures.
function reregisterError(code: string, message: string, retryable: boolean) {
  return {
    isAxiosError: true,
    response: { data: { detail: { code, message, retryable } } },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

describe('ReregisterDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useReregisterDialogStore.getState().close()
    get.mockResolvedValue({ data: makePendingServer({ id: 'srv-1' }) })
  })

  it('probes on open, shows the new fingerprint, and gates submit on the password', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({
      data: { server_id: 'srv-1', fingerprint_sha256: 'SHA256:newrebuiltfp' },
    })

    renderWithProviders(<ReregisterDialog />)
    openDialog()

    // Step 1: the new fingerprint is shown for the provider-console comparison.
    expect(await screen.findByText(/SHA256:newrebuiltfp/)).toBeInTheDocument()
    expect(post).toHaveBeenCalledWith('/servers/srv-1/reregister/probe')
    expect(
      screen.getByRole('button', { name: /copy fingerprint/i }),
    ).toBeInTheDocument()

    // Step 2: the single password field gates the submit.
    await user.click(
      screen.getByRole('button', { name: /fingerprint matches, continue/i }),
    )
    const password = screen.getByLabelText(
      /enter your server's root password/i,
    ) as HTMLInputElement
    expect(password.value).toBe('')
    const submit = screen.getByRole('button', { name: /^re-register$/i })
    expect(submit).toBeDisabled()
    await user.type(password, 'provider-pw')
    expect(submit).toBeEnabled()

    // The password step never mentions SSH keys.
    expect(screen.queryByText(/ssh key/i)).not.toBeInTheDocument()
  })

  it('completes and surfaces the harden-and-redeploy callout', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({
      data: { server_id: 'srv-1', fingerprint_sha256: 'SHA256:newrebuiltfp' },
    })
    post.mockResolvedValueOnce({
      data: makePendingServer({ id: 'srv-1', status: 'verified' }),
    })

    renderWithProviders(<ReregisterDialog />)
    openDialog()

    await user.click(
      await screen.findByRole('button', { name: /fingerprint matches, continue/i }),
    )
    await user.type(
      screen.getByLabelText(/enter your server's root password/i),
      'provider-pw',
    )
    await user.click(screen.getByRole('button', { name: /^re-register$/i }))

    expect(await screen.findByText(/server re-registered/i)).toBeInTheDocument()
    expect(screen.getByText(/run quick harden/i)).toBeInTheDocument()
    expect(post).toHaveBeenLastCalledWith('/servers/srv-1/reregister/complete', {
      password: 'provider-pw',
    })
  })

  it('shows friendly progress text from the polled state while submitting', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({
      data: { server_id: 'srv-1', fingerprint_sha256: 'SHA256:newrebuiltfp' },
    })
    // Hold the complete call open so the dialog stays on the submitting step.
    const pending = deferred<{ data: unknown }>()
    post.mockReturnValueOnce(pending.promise)
    // The progress poll reports the server mid password reset.
    get.mockResolvedValue({
      data: makePendingServer({ id: 'srv-1', reregistration_state: 'exchanging' }),
    })

    renderWithProviders(<ReregisterDialog />)
    openDialog()

    await user.click(
      await screen.findByRole('button', { name: /fingerprint matches, continue/i }),
    )
    await user.type(
      screen.getByLabelText(/enter your server's root password/i),
      'provider-pw',
    )
    await user.click(screen.getByRole('button', { name: /^re-register$/i }))

    expect(
      await screen.findByText(/password reset, handling it/i),
    ).toBeInTheDocument()

    // Let it finish so no act warnings linger.
    await act(async () => {
      pending.resolve({ data: makePendingServer({ id: 'srv-1', status: 'verified' }) })
    })
  })

  it('shows the mapped error copy, with a retry only when retryable', async () => {
    const user = userEvent.setup()
    // Probe, then a non-retryable AUTH_FAILED on complete.
    post.mockResolvedValueOnce({
      data: { server_id: 'srv-1', fingerprint_sha256: 'SHA256:newrebuiltfp' },
    })
    post.mockRejectedValueOnce(
      reregisterError('AUTH_FAILED', 'That password did not work.', false),
    )

    renderWithProviders(<ReregisterDialog />)
    openDialog()

    await user.click(
      await screen.findByRole('button', { name: /fingerprint matches, continue/i }),
    )
    await user.type(
      screen.getByLabelText(/enter your server's root password/i),
      'wrong-pw',
    )
    await user.click(screen.getByRole('button', { name: /^re-register$/i }))

    expect(
      await screen.findByText(/that password did not work/i),
    ).toBeInTheDocument()
    // AUTH_FAILED is not retryable: no retry button.
    expect(
      screen.queryByRole('button', { name: /try again/i }),
    ).not.toBeInTheDocument()
  })

  it('offers a retry for a retryable network failure', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({
      data: { server_id: 'srv-1', fingerprint_sha256: 'SHA256:newrebuiltfp' },
    })
    post.mockRejectedValueOnce(
      reregisterError(
        'NETWORK_UNREACHABLE',
        'Could not reach the server. Check that it is powered on and try again.',
        true,
      ),
    )

    renderWithProviders(<ReregisterDialog />)
    openDialog()

    await user.click(
      await screen.findByRole('button', { name: /fingerprint matches, continue/i }),
    )
    await user.type(
      screen.getByLabelText(/enter your server's root password/i),
      'provider-pw',
    )
    await user.click(screen.getByRole('button', { name: /^re-register$/i }))

    expect(await screen.findByText(/could not reach the server/i)).toBeInTheDocument()
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /try again/i }),
      ).toBeInTheDocument(),
    )
  })
})
