// App shell: registers the Clerk token getter for the axios client, defines the
// routes (public sign in / sign up, protected servers page), and mounts the global
// sonner Toaster. Providers (Clerk, Query, Router) live in main.tsx.

import { useAuth } from '@clerk/clerk-react'
import { useEffect } from 'react'
import { Route, Routes } from 'react-router-dom'

import { ProtectedRoute } from '@/components/ProtectedRoute'
import { Toaster } from '@/components/ui/sonner'
import { setTokenGetter } from '@/lib/auth-token'
import { ProjectsPage } from '@/pages/ProjectsPage'
import { ServerDetailPage } from '@/pages/ServerDetailPage'
import { ServersPage } from '@/pages/ServersPage'
import { SignInPage } from '@/pages/SignInPage'
import { SignUpPage } from '@/pages/SignUpPage'

function App() {
  const { getToken } = useAuth()

  // Bridge Clerk's getToken into the module level axios interceptor.
  useEffect(() => {
    setTokenGetter(() => getToken())
  }, [getToken])

  return (
    <>
      <Routes>
        <Route path="/sign-in/*" element={<SignInPage />} />
        <Route path="/sign-up/*" element={<SignUpPage />} />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <ServersPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/servers/:id"
          element={
            <ProtectedRoute>
              <ServerDetailPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/projects"
          element={
            <ProtectedRoute>
              <ProjectsPage />
            </ProtectedRoute>
          }
        />
      </Routes>
      {/* Global toast outlet, mounted once at the root. */}
      <Toaster richColors />
    </>
  )
}

export default App
