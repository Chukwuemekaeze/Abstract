import { UserButton, useUser } from '@clerk/clerk-react'
import { NavLink } from 'react-router-dom'
import { Trash2 } from 'lucide-react'

import { cn } from '@/lib/utils'
import { clerkAppearance } from '@/lib/clerkAppearance'
import { DeleteAccountDialog } from '@/components/DeleteAccountDialog'
import { useDeleteAccountDialogStore } from '@/store/delete-account-dialog'

// Top bar with nav links, the signed in user's email, and Clerk's avatar
// dropdown (account management and sign out). The UserButton is themed to
// match the shadcn surface. A custom "Delete account" item is added to the menu:
// Clerk's own self-serve deletion is disabled, so deletion goes through our
// backend (see DeleteAccountDialog) which tears down every server and keeps our
// DB and Clerk in sync.
export function Header() {
  const { user } = useUser()
  const openDeleteAccount = useDeleteAccountDialogStore((s) => s.openDialog)

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    cn(
      'text-sm hover:text-foreground',
      isActive ? 'text-foreground font-medium' : 'text-muted-foreground',
    )

  return (
    <header className="border-b">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
        <div className="flex items-center gap-6">
          <NavLink to="/" aria-label="Abstract home">
            <img
              src="/brand/abstract-lockup-glow.svg"
              alt="Abstract"
              className="h-8 w-auto"
            />
          </NavLink>
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
          <UserButton appearance={clerkAppearance}>
            <UserButton.MenuItems>
              <UserButton.Action
                label="Delete account"
                labelIcon={<Trash2 className="size-4" />}
                onClick={openDeleteAccount}
              />
            </UserButton.MenuItems>
          </UserButton>
        </div>
      </div>
      <DeleteAccountDialog />
    </header>
  )
}
