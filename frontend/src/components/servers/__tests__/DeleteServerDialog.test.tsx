import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
  connectionFailureMessage: () => null,
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import { apiClient } from '@/api/client'
import { DeleteServerDialog } from '@/components/servers/DeleteServerDialog'
import { useDeleteServerDialogStore } from '@/store/delete-server-dialog'
import { makePendingServer, renderWithProviders } from '@/test/utils'

const get = apiClient.get as unknown as Mock
const del = apiClient.delete as unknown as Mock

const REVEALED = 'Fr3sh$Root-Pw-9273'

function stepRow(name: string) {
  return { name, status: 'completed', detail: null, project_id: null, project_name: null }
}

describe('DeleteServerDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useDeleteServerDialogStore.setState({
      open: false,
      serverId: null,
      recordsOnly: false,
    })
    // The dialog fetches the server (for the name confirm) and the deletion preview.
    get.mockImplementation((url: string) =>
      url.endsWith('/deletion_preview')
        ? Promise.resolve({ data: { projects: [] } })
        : Promise.resolve({
            data: makePendingServer({ id: 'srv-1', name: 'web1', status: 'verified' }),
          }),
    )
  })

  async function confirmAndDelete(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<DeleteServerDialog />)
    const input = await screen.findByRole('textbox')
    await user.type(input, 'web1')
    await user.click(screen.getByRole('button', { name: 'Delete server' }))
  }

  it('reveals the one-time root password and gates Done on acknowledgment', async () => {
    const user = userEvent.setup()
    useDeleteServerDialogStore.getState().openWith('srv-1')
    del.mockResolvedValueOnce({
      data: {
        success: true,
        steps: [stepRow('restore_ssh_access'), stepRow('reset_root_password')],
        revealed_root_password: REVEALED,
      },
    })

    await confirmAndDelete(user)

    // The password is shown and the dialog stays open (not navigated away).
    expect(await screen.findByText(REVEALED)).toBeInTheDocument()
    expect(useDeleteServerDialogStore.getState().open).toBe(true)

    // Done is disabled until the save is acknowledged.
    const done = screen.getByRole('button', { name: 'Done' })
    expect(done).toBeDisabled()

    // Copy writes the exact password to the clipboard.
    await user.click(screen.getByRole('button', { name: /copy password/i }))
    expect(await navigator.clipboard.readText()).toBe(REVEALED)

    // Acknowledging enables Done, which closes the dialog.
    await user.click(screen.getByRole('checkbox'))
    expect(done).toBeEnabled()
    await user.click(done)
    await waitFor(() =>
      expect(useDeleteServerDialogStore.getState().open).toBe(false),
    )
  })

  it('closes immediately when no password is revealed', async () => {
    const user = userEvent.setup()
    useDeleteServerDialogStore.getState().openWith('srv-1')
    del.mockResolvedValueOnce({
      data: {
        success: true,
        steps: [stepRow('restore_ssh_access')],
        revealed_root_password: null,
      },
    })

    await confirmAndDelete(user)

    await waitFor(() =>
      expect(useDeleteServerDialogStore.getState().open).toBe(false),
    )
    expect(screen.queryByText(/New root password/)).not.toBeInTheDocument()
  })
})
