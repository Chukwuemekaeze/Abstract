// Servers page: the fleet table plus the multi-step add dialog and the cancel
// registration dialog (both read their open state from stores). Rendered inside
// the AppShell layout, so no header of its own.

import { AddServerDialog } from '@/components/AddServerDialog'
import { ServersTable } from '@/components/servers/ServersTable'
import { CancelRegistrationDialog } from '@/components/servers/CancelRegistrationDialog'
import { Button } from '@/components/ui/button'
import { useAddServerStore } from '@/store/addServerStore'

export function ServersPage() {
  const openAddServer = useAddServerStore((s) => s.open)

  return (
    <div className="mx-auto max-w-6xl px-8 py-10">
      <header className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">Servers</h1>
          <p className="mt-1 text-sm text-text-dim">
            Register a VPS, verify its host key, and deploy.
          </p>
        </div>
        <Button onClick={openAddServer}>Add server</Button>
      </header>

      <ServersTable />
      <AddServerDialog />
      <CancelRegistrationDialog />
    </div>
  )
}
