// App shell: registers the Clerk token getter for the axios client, defines the
// routes (public sign in / sign up, protected servers page), and mounts the global
// sonner Toaster. Providers (Clerk, Query, Router) live in main.tsx.

import { useAuth } from '@clerk/clerk-react'
import * as Sentry from '@sentry/react'
import { useEffect } from 'react'
import { Route, Routes } from 'react-router-dom'

import { useCurrentUserQuery } from '@/api/account'
import { AppShell } from '@/components/AppShell'
import { ProtectedRoute } from '@/components/ProtectedRoute'
import { Toaster } from '@/components/ui/sonner'
import { clearUser, setUser } from '@/integrations/monitoring'
import { setTokenGetter } from '@/lib/auth-token'
import { ProjectsPage } from '@/pages/ProjectsPage'
import { ServerDetailPage } from '@/pages/ServerDetailPage'
import { ServersPage } from '@/pages/ServersPage'
import { SignInPage } from '@/pages/SignInPage'
import { SignUpPage } from '@/pages/SignUpPage'

// Route tracing: naming navigation/pageload transactions after the matched route
// pattern requires wrapping Routes with Sentry's routing instrumentation (paired
// with reactRouterBrowserTracingIntegration in monitoring.ts).
const SentryRoutes = Sentry.wrapReactRouterRouting(Routes)

function App() {
  const { getToken, isSignedIn } = useAuth()

  // Bridge Clerk's getToken into the module level axios interceptor.
  useEffect(() => {
    setTokenGetter(() => getToken())
  }, [getToken])

  // Attribute monitoring events to the backend Postgres user id, mirroring the
  // backend's _bind_identity. Fetch it only once Clerk reports the user is signed
  // in; clear it on sign-out so later events carry no user.
  const { data: currentUser } = useCurrentUserQuery(Boolean(isSignedIn))
  useEffect(() => {
    if (currentUser) {
      setUser(currentUser.id)
    } else if (!isSignedIn) {
      clearUser()
    }
  }, [currentUser, isSignedIn])

  return (
    <>
      <SentryRoutes>
        <Route path="/sign-in/*" element={<SignInPage />} />
        <Route path="/sign-up/*" element={<SignUpPage />} />
        {/* Protected routes share the sidebar shell via a layout route. */}
        <Route
          element={
            <ProtectedRoute>
              <AppShell />
            </ProtectedRoute>
          }
        >
          <Route path="/" element={<ServersPage />} />
          <Route path="/servers/:id" element={<ServerDetailPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
        </Route>
      </SentryRoutes>
      {/* Global toast outlet, mounted once at the root. */}
      <Toaster richColors />
    </>
  )
}

export default App
