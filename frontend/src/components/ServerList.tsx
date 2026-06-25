// Renders the user's servers, or an empty state CTA when there are none.

import { Loader2 } from 'lucide-react'

import { useServers } from '@/api/servers'
import { ServerCard } from '@/components/ServerCard'
import { Button } from '@/components/ui/button'
import { useAddServerStore } from '@/store/addServerStore'

export function ServerList() {
  const { data: servers, isLoading, isError } = useServers()
  const openAddServer = useAddServerStore((s) => s.open)

  if (isLoading) {
    return (
      <div className="flex items-center gap-3 py-12 text-muted-foreground">
        <Loader2 className="size-5 animate-spin" />
        <span>Loading servers...</span>
      </div>
    )
  }

  if (isError) {
    return (
      <p className="py-12 text-destructive">
        Could not load servers. Is the backend running?
      </p>
    )
  }

  // Empty state: invite the user to add their first server.
  if (!servers || servers.length === 0) {
    return (
      <div className="flex flex-col items-center gap-4 rounded-lg border border-dashed py-16">
        <p className="text-muted-foreground">No servers yet.</p>
        <Button onClick={openAddServer}>Add your first server</Button>
      </div>
    )
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {servers.map((server) => (
        <ServerCard key={server.id} server={server} />
      ))}
    </div>
  )
}
