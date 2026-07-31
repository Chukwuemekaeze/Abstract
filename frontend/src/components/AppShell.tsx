// The app frame: a fixed left sidebar (brand lockup, primary nav, and the
// signed-in user) plus a scrollable content area that renders the active route
// via <Outlet />.
//
// Only the two routes that actually exist are linked here; nav items from the
// mockups with no backend (Deploy keys, Activity, Settings) are intentionally
// omitted. Account deletion lives in the Clerk user menu, as before.

import { UserButton, useUser } from '@clerk/clerk-react'
import { NavLink, Outlet } from 'react-router-dom'
import { FolderGit2, Server, Trash2 } from 'lucide-react'

import { clerkAppearance } from '@/lib/clerkAppearance'
import { cn } from '@/lib/utils'
import { DeleteAccountDialog } from '@/components/DeleteAccountDialog'
import { useDeleteAccountDialogStore } from '@/store/delete-account-dialog'

const navItems = [
  { to: '/', label: 'Servers', icon: Server, end: true },
  { to: '/projects', label: 'Projects', icon: FolderGit2, end: false },
]

export function AppShell() {
  const { user } = useUser()
  const openDeleteAccount = useDeleteAccountDialogStore((s) => s.openDialog)

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    cn(
      'flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors',
      isActive
        ? 'bg-card font-medium text-foreground'
        : 'text-text-dim hover:bg-card/60 hover:text-foreground',
    )

  return (
    <div className="flex h-screen w-full overflow-hidden pb-0.5">
      <aside className="flex w-60 shrink-0 flex-col">
        {/* Brand + primary nav: one bordered panel, grows to fill the column. */}
        <div className="flex flex-1 flex-col gap-3 border-2 border-cyan/30 bg-surface px-3 py-4">
          <NavLink to="/" aria-label="Abstract home">
            <img
              src="/brand/abstract-lockup-glow.svg"
              alt="Abstract"
              // Sized so the "abstract" wordmark reads at ~120% of the text-sm
              // nav labels below. The lockup is 640x240 with the word set at
              // font-size 98, so height = 1.2 * 0.875rem * 240 / 98 ~= 2.57rem.
              className="h-[3.57rem] w-auto"
            />
          </NavLink>

          <nav className="flex flex-col gap-1">
            {navItems.map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} className={navLinkClass}>
                <Icon className="size-4" />
                {label}
              </NavLink>
            ))}
          </nav>
        </div>

        {/* Signed-in user: its own bordered panel, sharing the edge above. */}
        <div className="-mt-0.5 flex items-center gap-3 border-2 border-cyan/30 bg-surface px-3 py-3">
          <UserButton appearance={clerkAppearance}>
            <UserButton.MenuItems>
              <UserButton.Action
                label="Delete account"
                labelIcon={<Trash2 className="size-4" />}
                onClick={openDeleteAccount}
              />
            </UserButton.MenuItems>
          </UserButton>
          {user?.primaryEmailAddress?.emailAddress && (
            <span className="truncate text-sm text-text-dim">
              {user.primaryEmailAddress.emailAddress}
            </span>
          )}
        </div>
      </aside>

      {/* Main content: the whole pane is one bordered panel, sharing the sidebar edge. */}
      <main className="-ml-0.5 flex-1 overflow-y-auto border-2 border-cyan/30 bg-surface">
        <Outlet />
      </main>

      <DeleteAccountDialog />
    </div>
  )
}
