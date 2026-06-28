import { UserButton, useUser } from '@clerk/clerk-react'

import { clerkAppearance } from '@/lib/clerkAppearance'

// Top bar with the signed in user's email and Clerk's avatar dropdown (account
// management and sign out). The UserButton is themed to match the shadcn surface.
export function Header() {
  const { user } = useUser()

  return (
    <header className="border-b">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
        <h1 className="text-lg font-semibold">Abstract</h1>
        <div className="flex items-center gap-3">
          {user?.primaryEmailAddress?.emailAddress && (
            <span className="text-muted-foreground text-sm">
              {user.primaryEmailAddress.emailAddress}
            </span>
          )}
          <UserButton appearance={clerkAppearance} />
        </div>
      </div>
    </header>
  )
}
