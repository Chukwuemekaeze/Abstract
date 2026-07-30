// The app frame: a fixed left sidebar (brand lockup, primary nav, a small
// context-aware summary of counts, and the signed-in user) plus a scrollable
// content area that renders the active route via <Outlet />.
//
// Counts are derived client-side from the already-cached servers and projects
// queries, so the sidebar stays in sync with the tables without extra requests.
// Only the two routes that actually exist are linked here; nav items from the
// mockups with no backend (Deploy keys, Activity, Settings) are intentionally
// omitted. Account deletion lives in the Clerk user menu, as before.

import { UserButton, useUser } from '@clerk/clerk-react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { FolderGit2, Server, Trash2 } from 'lucide-react'

import { useProjects } from '@/api/projects'
import { useServers } from '@/api/servers'
import { clerkAppearance } from '@/lib/clerkAppearance'
import { cn } from '@/lib/utils'
import { DeleteAccountDialog } from '@/components/DeleteAccountDialog'
import { useDeleteAccountDialogStore } from '@/store/delete-account-dialog'

const navItems = [
  { to: '/', label: 'Servers', icon: Server, end: true },
  { to: '/projects', label: 'Projects', icon: FolderGit2, end: false },
]

// One labelled count row in the sidebar summary. The value is tinted so the
// eye can pick out the "active" numbers (running, online) at a glance.
function SummaryRow({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone: 'cyan' | 'amber' | 'dim'
}) {
  const toneClass =
    tone === 'cyan'
      ? 'text-cyan'
      : tone === 'amber'
        ? 'text-amber-400'
        : 'text-text-dim'
  return (
    <div className="flex items-center justify-between px-3 py-1 text-sm">
      <span className="text-text-dim">{label}</span>
      <span className={cn('font-medium tabular-nums', toneClass)}>{value}</span>
    </div>
  )
}

export function AppShell() {
  const { user } = useUser()
  const openDeleteAccount = useDeleteAccountDialogStore((s) => s.openDialog)
  const { pathname } = useLocation()

  const servers = useServers()
  const projects = useProjects()

  const serverList = servers.data ?? []
  const projectList = projects.data ?? []

  // Fleet summary (servers routes).
  const online = serverList.filter((s) => s.status === 'verified').length
  const unverified = serverList.filter(
    (s) => s.status === 'pending_verification' || s.status === 'key_mismatch',
  ).length

  // Project summary (projects route).
  const deploying = projectList.filter((p) => p.active_operation != null).length
  const running = projectList.filter(
    (p) => p.runtime_status === 'running' && p.active_operation == null,
  ).length
  const stopped = projectList.length - running - deploying

  const onProjects = pathname.startsWith('/projects')

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    cn(
      'flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors',
      isActive
        ? 'bg-card text-foreground'
        : 'text-text-dim hover:bg-card/60 hover:text-foreground',
    )

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-surface">
        <div className="px-5 py-5">
          <NavLink to="/" aria-label="Abstract home">
            <img
              src="/brand/abstract-lockup-glow.svg"
              alt="Abstract"
              className="h-7 w-auto"
            />
          </NavLink>
        </div>

        <nav className="flex flex-col gap-1 px-3">
          {navItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={navLinkClass}>
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-8 px-3">
          <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-text-dim/70">
            {onProjects ? 'Projects' : 'Fleet'}
          </p>
          {onProjects ? (
            <>
              <SummaryRow label="Running" value={running} tone="cyan" />
              <SummaryRow label="Deploying" value={deploying} tone="amber" />
              <SummaryRow label="Stopped" value={stopped} tone="dim" />
            </>
          ) : (
            <>
              <SummaryRow label="Online" value={online} tone="cyan" />
              <SummaryRow label="Unverified" value={unverified} tone="amber" />
              <SummaryRow label="Projects" value={projectList.length} tone="dim" />
            </>
          )}
        </div>

        <div className="mt-auto flex items-center gap-3 border-t border-border px-4 py-3">
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

      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>

      <DeleteAccountDialog />
    </div>
  )
}
