// The single page of the v1 app: header with an Add Server button, the server
// list, and the multi-step add dialog (which reads its open state from the store).

import { AddServerDialog } from '@/components/AddServerDialog'
import { Header } from '@/components/Header'
import { ServerList } from '@/components/ServerList'
import { Button } from '@/components/ui/button'
import { useAddServerStore } from '@/store/addServerStore'

export function ServersPage() {
  const openAddServer = useAddServerStore((s) => s.open)

  return (
    <>
      <Header />
      <div className="mx-auto max-w-5xl px-6 py-10">
        <header className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">Servers</h1>
            <p className="text-muted-foreground text-sm">
              Register a VPS, verify its host key, and deploy.
            </p>
          </div>
          <Button onClick={openAddServer}>Add server</Button>
        </header>

        <ServerList />
        <AddServerDialog />
      </div>
    </>
  )
}
