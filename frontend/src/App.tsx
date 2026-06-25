// App shell: wires the TanStack Query provider, the router (single route for v1),
// and the global sonner Toaster.

import { QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { Toaster } from '@/components/ui/sonner'
import { queryClient } from '@/lib/queryClient'
import { ServersPage } from '@/pages/ServersPage'

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<ServersPage />} />
        </Routes>
      </BrowserRouter>
      {/* Global toast outlet, mounted once at the root. */}
      <Toaster richColors />
    </QueryClientProvider>
  )
}

export default App
