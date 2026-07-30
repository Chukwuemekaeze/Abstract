// The fleet table: one row per server (identity, address, project count, host
// key), with a Test action for verified servers and a Verify action for pending
// ones. Any server still awaiting its trust-on-first-use host key check gets a
// prominent card below the table that routes into the existing resume flow.
//
// Only fields the API actually returns are shown. The mockup's LOAD column and
// hardware spec line have no backing on the Server schema and are omitted, as is
// the recent-activity feed (no endpoint).

import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage } from '@/api/client'
import { useProjects } from '@/api/projects'
import {
  type Server,
  type ServerStatus,
  useServers,
  useSmokeTestMutation,
} from '@/api/servers'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useAddServerStore } from '@/store/addServerStore'
import { useCancelRegistrationDialogStore } from '@/store/cancel-registration-dialog'

const STATUS_DOT: Record<ServerStatus, string> = {
  verified: 'bg-green-500',
  pending_verification: 'bg-amber-400',
  key_mismatch: 'bg-red-500',
}

function truncateFingerprint(fp: string | null): string {
  if (!fp) return 'unknown'
  return fp.length <= 26 ? fp : `${fp.slice(0, 26)}...`
}

function ServerRow({
  server,
  projectCount,
}: {
  server: Server
  projectCount: number
}) {
  const smokeTest = useSmokeTestMutation()
  const resume = useAddServerStore((s) => s.resume)
  const isPending = server.status === 'pending_verification'

  const runTest = async () => {
    try {
      const result = await smokeTest.mutateAsync(server.id)
      toast.success(`Smoke test passed (exit ${result.exit_status}).`)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Smoke test failed.'))
    }
  }

  return (
    <div className="grid grid-cols-[2fr_1.5fr_0.6fr_1.6fr_auto] items-center gap-4 border-b border-border px-4 py-4 text-sm">
      <div className="flex items-center gap-2.5">
        <span className={cn('size-2 shrink-0 rounded-full', STATUS_DOT[server.status])} />
        <span className="font-medium text-foreground">{server.name}</span>
      </div>
      <span className="font-mono text-xs text-text-dim">
        {server.host}:{server.port}
      </span>
      <span className="tabular-nums text-text-dim">{projectCount}</span>
      <span className="truncate font-mono text-xs text-text-dim">
        {truncateFingerprint(server.fingerprint_sha256)}
      </span>
      <div className="justify-self-end">
        {isPending ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => resume(server)}
          >
            Verify
          </Button>
        ) : (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={runTest}
            disabled={server.status !== 'verified' || smokeTest.isPending}
          >
            {smokeTest.isPending && (
              <Loader2 className="mr-1.5 size-3.5 animate-spin" />
            )}
            Test
          </Button>
        )}
      </div>
    </div>
  )
}

// The trust-on-first-use prompt. Confirming resumes registration (which shows
// the full fingerprint and collects the password to install the app key);
// rejecting opens the cancel-registration dialog.
function HostKeyCheckCard({ server }: { server: Server }) {
  const resume = useAddServerStore((s) => s.resume)
  const openCancel = useCancelRegistrationDialogStore((s) => s.openWith)

  return (
    <div className="mt-6 rounded-lg border border-amber-500/40 p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-base font-semibold text-foreground">
            {server.name} needs a host key check
          </h3>
          <p className="mt-1 max-w-xl text-sm text-text-dim">
            Abstract fetched this fingerprint on first contact. Confirm it
            matches what your provider shows and the machine becomes deployable.
          </p>
          {server.fingerprint_sha256 && (
            <code className="mt-3 inline-block rounded-md border border-border bg-card px-3 py-2 font-mono text-xs text-text-dim">
              {server.fingerprint_sha256}
            </code>
          )}
        </div>
        <div className="flex shrink-0 flex-col gap-2">
          <Button type="button" onClick={() => resume(server)}>
            Matches, verify
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => openCancel(server.id)}
          >
            It does not match
          </Button>
        </div>
      </div>
    </div>
  )
}

export function ServersTable() {
  const { data: servers, isLoading, isError } = useServers()
  const projects = useProjects()
  const openAddServer = useAddServerStore((s) => s.open)

  if (isLoading) {
    return (
      <div className="flex items-center gap-3 py-12 text-text-dim">
        <Loader2 className="size-5 animate-spin" />
        <span>Loading servers...</span>
      </div>
    )
  }

  if (isError) {
    return (
      <p className="py-12 text-destructive">
        Something went wrong while loading your servers. Please try again later.
      </p>
    )
  }

  if (!servers || servers.length === 0) {
    return (
      <div className="flex flex-col items-center gap-4 rounded-lg border border-dashed border-border py-16">
        <p className="font-display text-lg font-bold tracking-[-0.02em] text-text-dim">
          No servers yet.
        </p>
        <Button onClick={openAddServer}>Add your first server</Button>
      </div>
    )
  }

  // Project counts per server, derived from the projects list.
  const countByServer = new Map<string, number>()
  for (const p of projects.data ?? []) {
    countByServer.set(p.server_id, (countByServer.get(p.server_id) ?? 0) + 1)
  }

  const pending = servers.filter((s) => s.status === 'pending_verification')

  return (
    <div>
      <div className="grid grid-cols-[2fr_1.5fr_0.6fr_1.6fr_auto] gap-4 border-b border-border px-4 pb-2 text-xs font-semibold uppercase tracking-[0.06em] text-text-dim">
        <span>Server</span>
        <span>Address</span>
        <span>Projects</span>
        <span>Host key</span>
        <span className="justify-self-end">Action</span>
      </div>

      {servers.map((server) => (
        <ServerRow
          key={server.id}
          server={server}
          projectCount={countByServer.get(server.id) ?? 0}
        />
      ))}

      {pending.map((server) => (
        <HostKeyCheckCard key={server.id} server={server} />
      ))}
    </div>
  )
}
