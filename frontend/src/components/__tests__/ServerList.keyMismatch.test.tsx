// Regression test for the "key mismatch not reflected until refresh" bug: when a
// smoke test detects a changed host key, the badge must flip to "key mismatch" on
// its own, driven by the global MutationCache handler in @/lib/queryClient (which
// invalidates the servers cache on the tagged 409). Uses the real queryClient so
// that handler actually runs.

import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// Mock the axios client so useServers/useSmokeTestMutation resolve without the
// network. Note: we do NOT mock 'axios' itself — the handler's axios.isAxiosError
// check must run against the rejected error below.
vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  extractErrorMessage: (_err: unknown, fallback: string) => fallback,
}))

// Toasts need a mounted <Toaster>; stub them out.
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import { apiClient } from '@/api/client'
import { ServerList } from '@/components/ServerList'
import { queryClient } from '@/lib/queryClient'
import { makePendingServer } from '@/test/utils'

const get = apiClient.get as unknown as Mock
const post = apiClient.post as unknown as Mock

function renderList() {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ServerList />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ServerList — a smoke test that hits a key mismatch', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    queryClient.clear()
  })

  it('flips the badge to "key mismatch" without a manual refresh', async () => {
    const user = userEvent.setup()
    const verified = makePendingServer({
      id: 'srv-1',
      name: 'web1',
      status: 'verified',
    })
    const mismatched = { ...verified, status: 'key_mismatch' as const }

    // First load shows the verified server; the refetch triggered by the mismatch
    // returns the status the backend has already persisted.
    get.mockResolvedValueOnce({ data: [verified] })
    get.mockResolvedValue({ data: [mismatched] })

    // The smoke test connects, meets the changed host key, and the backend returns a
    // 409 tagged with the machine-readable header the handler keys off.
    post.mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 409,
        data: { detail: 'host key mismatch' },
        headers: { 'x-error-code': 'host_key_mismatch' },
      },
    })

    renderList()

    expect(await screen.findByText('verified')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /run smoke test/i }))

    // No page refresh: the invalidated cache refetches and the badge updates itself.
    expect(await screen.findByText('key mismatch')).toBeInTheDocument()
    expect(screen.queryByText('verified')).not.toBeInTheDocument()
  })
})
