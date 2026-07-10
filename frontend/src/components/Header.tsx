import { UserButton, useUser } from '@clerk/clerk-react'
import { NavLink } from 'react-router-dom'

import { cn } from '@/lib/utils'
import { clerkAppearance } from '@/lib/clerkAppearance'

// Top bar with nav links, the signed in user's email, and Clerk's avatar
// dropdown (account management and sign out). The UserButton is themed to
// match the shadcn surface.
export function Header() {
  const { user } = useUser()

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    cn(
      'text-sm hover:text-foreground',
      isActive ? 'text-foreground font-medium' : 'text-muted-foreground',
    )

  return (
    <header className="border-b">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
        <div className="flex items-center gap-6">
          <h1 className="text-lg font-semibold">Abstract</h1>
          <nav className="flex items-center gap-4">
            <NavLink to="/" end className={navLinkClass}>
              Servers
            </NavLink>
            <NavLink to="/projects" className={navLinkClass}>
              Projects
            </NavLink>
          </nav>
        </div>
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
