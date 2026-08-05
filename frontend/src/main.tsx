import { ClerkProvider } from '@clerk/clerk-react'
import * as Sentry from '@sentry/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { initMonitoring } from '@/integrations/monitoring'
import { queryClient } from '@/lib/queryClient'
import App from './App.tsx'
// Self-hosted brand fonts. Space Grotesk (display) at the two weights we use;
// Geist Sans and Geist Mono as variable fonts. Imported before the stylesheet so
// the @font-face rules are registered when the theme tokens reference them.
import '@fontsource/space-grotesk/500.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import './index.css'

const PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY

if (!PUBLISHABLE_KEY) {
  throw new Error('Missing VITE_CLERK_PUBLISHABLE_KEY')
}

// Initialize monitoring before the app mounts, mirroring the backend wiring
// Sentry at startup in main.py. A no-op when no DSN is configured.
initMonitoring()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Catch render errors and report them to Sentry — the client counterpart
        to the backend auto-capturing unhandled request errors. */}
    <Sentry.ErrorBoundary
      fallback={
        <div style={{ padding: '2rem', textAlign: 'center' }}>
          Something went wrong. Please reload the page.
        </div>
      }
    >
      <ClerkProvider publishableKey={PUBLISHABLE_KEY} afterSignOutUrl="/sign-in">
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </ClerkProvider>
    </Sentry.ErrorBoundary>
  </StrictMode>,
)
