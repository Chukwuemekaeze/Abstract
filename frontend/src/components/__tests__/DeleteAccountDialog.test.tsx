import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AxiosError } from 'axios'

vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

const signOut = vi.fn()
vi.mock('@clerk/clerk-react', () => ({
  useClerk: () => ({ signOut }),
}))

import { apiClient } from '@/api/client'
import { DeleteAccountDialog } from '@/components/DeleteAccountDialog'
import { useDeleteAccountDialogStore } from '@/store/delete-account-dialog'
import { renderWithProviders } from '@/test/utils'

const del = apiClient.delete as unknown as Mock

async function armAndClick() {
  const user = userEvent.setup()
  // The delete button is gated on the type-to-confirm phrase.
  await user.type(
    screen.getByLabelText(/to confirm/i),
    'delete my account',
  )
  await user.click(screen.getByRole('button', { name: 'Delete account' }))
  return user
}

function blockingServerError() {
  const err = new AxiosError('Request failed', 'ERR_BAD_REQUEST')
  // The structured 409 the backend returns when a server can't be reached: the raw
  // message names the internal 'connect_ssh' step, which we must not surface.
  err.response = {
    status: 409,
    data: {
      detail: {
        message:
          "Server 'web1' could not be torn down (Deletion failed at step 'connect_ssh'.). Resolve or delete that server, then delete your account again.",
        blocking_server_id: 'srv-1',
        blocking_server_name: 'web1',
        steps: [
          {
            name: 'connect_ssh',
            status: 'failed',
            detail: 'Could not connect to the server: [Errno 110] Connection timed out',
          },
        ],
      },
    },
  } as never
  return err
}

describe('DeleteAccountDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useDeleteAccountDialogStore.setState({ open: true })
  })

  it('does not delete until the confirm phrase is typed', async () => {
    renderWithProviders(<DeleteAccountDialog />)

    // Armed only by the phrase: the button is disabled and nothing is sent.
    expect(screen.getByRole('button', { name: 'Delete account' })).toBeDisabled()
    expect(del).not.toHaveBeenCalled()
  })

  it('deletes against the backend and signs the user out on success', async () => {
    del.mockResolvedValueOnce({ data: { success: true } })
    renderWithProviders(<DeleteAccountDialog />)

    await armAndClick()

    await waitFor(() => expect(del).toHaveBeenCalledWith('/account'))
    expect(del).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(signOut).toHaveBeenCalledTimes(1))
  })

  it('surfaces a clean unreachable message and keeps the user signed in on a 409', async () => {
    del.mockRejectedValueOnce(blockingServerError())
    renderWithProviders(<DeleteAccountDialog />)

    await armAndClick()

    // The clean message names the server and the fix, without the internal step name.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/Server 'web1' couldn't be reached/)
    expect(alert).toHaveTextContent(/powered on and online/)
    expect(alert).not.toHaveTextContent(/connect_ssh/)
    expect(alert).not.toHaveTextContent(/step/)
    // The user is not signed out and the dialog stays open for a retry.
    expect(signOut).not.toHaveBeenCalled()
    expect(useDeleteAccountDialogStore.getState().open).toBe(true)
  })
})
