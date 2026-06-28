import { useAuth } from '@clerk/clerk-react'
import { Loader2Icon } from 'lucide-react'
import type { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'

// Gates a route on Clerk auth: shows a spinner while Clerk loads, redirects to
// sign in when signed out, otherwise renders the protected content.
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn } = useAuth()

  if (!isLoaded) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Loader2Icon className="text-muted-foreground size-6 animate-spin" />
      </div>
    )
  }

  if (!isSignedIn) {
    return <Navigate to="/sign-in" replace />
  }

  return <>{children}</>
}
