import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'

// Mock the axios client so useServer resolves without the network.
vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
  extractHardeningError: () => ({ message: 'err', output: null }),
  extractServerDeletionError: (_err: unknown, fallback: string) => ({
    message: fallback,
    failedStep: null,
    steps: [],
  }),
}))

// Header pulls in Clerk, which needs a provider we don't set up here.
vi.mock('@/components/Header', () => ({ Header: () => null }))

import { apiClient } from '@/api/client'
import { ServerDetailPage } from '@/pages/ServerDetailPage'
import { useReRegisterServerDialogStore } from '@/store/reregister-server-dialog'
import { useDeleteServerDialogStore } from '@/store/delete-server-dialog'
import { makePendingServer } from '@/test/utils'

const get = apiClient.get as unknown as Mock

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(<ServerDetailPage />, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/servers/srv-1']}>
          <Routes>
            <Route path="/servers/:id" element={children} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    ),
  })
}

describe('ServerDetailPage — key_mismatch', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useReRegisterServerDialogStore.getState().close()
    useDeleteServerDialogStore.getState().close()
    get.mockResolvedValue({
      data: makePendingServer({ id: 'srv-1', name: 'web1', status: 'key_mismatch' }),
    })
  })

  it('warns the host key changed, blocks operations, and offers the two actions', async () => {
    renderPage()

    // The destructive banner explains the identity can no longer be verified.
    expect(
      await screen.findByText(/identity can no longer be verified/i),
    ).toBeInTheDocument()

    // Both explicit actions are present.
    expect(
      screen.getByRole('button', { name: /re-register this server/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /remove server record/i }),
    ).toBeInTheDocument()

    // Operations are blocked: none of the hardening cards render for this state.
    expect(screen.queryByText('Operations')).not.toBeInTheDocument()
    expect(screen.queryByText('Update system')).not.toBeInTheDocument()
  })

  it('opens the re-register dialog from the action button', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /re-register this server/i }),
    )
    expect(useReRegisterServerDialogStore.getState().open).toBe(true)
    expect(useReRegisterServerDialogStore.getState().serverId).toBe('srv-1')
  })
})
