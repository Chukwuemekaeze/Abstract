// Shared test helpers: render a component inside the providers the app relies on
// (react-query for the API hooks, a router for useNavigate/Link), plus a factory
// for the Server shape the pending-registration UI reads.

import type { ReactElement, ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { render } from '@testing-library/react'

import type { Server } from '@/api/servers'

export function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(ui, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    ),
  })
}

export function makePendingServer(overrides: Partial<Server> = {}): Server {
  return {
    id: 'srv-1',
    name: 'pending-web',
    host: '203.0.113.50',
    port: 22,
    username: 'root',
    status: 'pending_verification',
    active_operation: null,
    fingerprint_sha256: 'SHA256:abcdef0123456789abcdef0123456789',
    host_key_type: 'ssh-ed25519',
    password_auth_disabled: false,
    verification_source: 'tofu',
    created_at: '2026-07-20T00:00:00Z',
    verified_at: null,
    sudo_user_name: null,
    root_login_disabled: false,
    firewall_enabled: false,
    docker_installed: false,
    base_packages_installed: false,
    nginx_installed: false,
    swap_configured: false,
    last_system_update_at: null,
    ...overrides,
  }
}
