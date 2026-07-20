import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AxiosError } from 'axios'

vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

import { apiClient } from '@/api/client'
import { CancelRegistrationDialog } from '@/components/servers/CancelRegistrationDialog'
import { useCancelRegistrationDialogStore } from '@/store/cancel-registration-dialog'
import { makePendingServer, renderWithProviders } from '@/test/utils'

const get = apiClient.get as unknown as Mock
const post = apiClient.post as unknown as Mock

function cleanupFailureError() {
  const err = new AxiosError('Request failed', 'ERR_BAD_RESPONSE')
  // The structured 502 body the backend returns when remote key removal fails.
  err.response = {
    data: {
      detail: {
        message: "Cancellation failed at step 'remove_authorized_key'.",
        failed_step: 'remove_authorized_key',
        failed_project_id: null,
        failed_project_name: null,
        steps: [
          {
            name: 'restore_ssh_access',
            status: 'completed',
            detail: null,
            project_id: null,
            project_name: null,
          },
          {
            name: 'remove_authorized_key',
            status: 'failed',
            detail: 'could not connect to the server',
            project_id: null,
            project_name: null,
          },
        ],
      },
    },
  } as never
  return err
}

describe('CancelRegistrationDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useCancelRegistrationDialogStore.setState({ open: false, serverId: null })
    get.mockResolvedValue({ data: makePendingServer({ id: 'srv-1', name: 'pending-web' }) })
  })

  it('only calls the cancel endpoint after the user confirms', async () => {
    const user = userEvent.setup()
    useCancelRegistrationDialogStore.getState().openWith('srv-1')
    post.mockResolvedValueOnce({
      data: {
        success: true,
        steps: [
          {
            name: 'delete_server_record',
            status: 'completed',
            detail: null,
            project_id: null,
            project_name: null,
          },
        ],
      },
    })

    renderWithProviders(<CancelRegistrationDialog />)

    // Confirmation is required: nothing is sent just by opening the dialog.
    const confirm = await screen.findByRole('button', { name: 'Cancel registration' })
    expect(post).not.toHaveBeenCalled()

    await user.click(confirm)

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/servers/srv-1/cancel'),
    )
    expect(post).toHaveBeenCalledTimes(1)
    // On success the dialog closes.
    await waitFor(() =>
      expect(useCancelRegistrationDialogStore.getState().open).toBe(false),
    )
  })

  it('reports the failed cleanup outcome and keeps the dialog open for retry', async () => {
    const user = userEvent.setup()
    useCancelRegistrationDialogStore.getState().openWith('srv-1')
    post.mockRejectedValueOnce(cleanupFailureError())

    renderWithProviders(<CancelRegistrationDialog />)

    await user.click(
      await screen.findByRole('button', { name: 'Cancel registration' }),
    )

    // The reported per-step outcome is surfaced (never a silent success).
    expect(
      await screen.findByText(/Cancellation failed at step 'remove_authorized_key'/),
    ).toBeInTheDocument()
    expect(screen.getByText("Remove Abstract's SSH key")).toBeInTheDocument()
    expect(screen.getByText(/Nothing was deleted/)).toBeInTheDocument()

    // The dialog stays open with a retry, and the record was not removed from the UI.
    expect(useCancelRegistrationDialogStore.getState().open).toBe(true)
    expect(
      screen.getByRole('button', { name: /retry cancellation/i }),
    ).toBeInTheDocument()
  })
})
