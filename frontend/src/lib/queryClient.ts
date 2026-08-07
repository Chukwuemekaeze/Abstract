import { MutationCache, QueryClient } from '@tanstack/react-query'
import axios from 'axios'

import { serverKeys } from '@/api/servers'

// When any mutation fails because the server presented a changed host key, the
// backend has already flipped that server's status to "key_mismatch" and tags the
// 409 with an X-Error-Code header. Refetch the servers cache so the list badge and
// the open detail page reflect the mismatch immediately, without a manual refresh.
// Invalidating serverKeys.all is prefix based, so it also refreshes every
// ['servers', id] detail query.
const mutationCache = new MutationCache({
  onError: (error) => {
    if (
      axios.isAxiosError(error) &&
      error.response?.headers['x-error-code'] === 'host_key_mismatch'
    ) {
      queryClient.invalidateQueries({ queryKey: serverKeys.all })
    }
  },
})

export const queryClient = new QueryClient({
  mutationCache,
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      // Default staleTime of 0 refetches on every mount/navigation/dialog open,
      // producing many duplicate requests. 30s serves the cache instead;
      // mutations still call invalidateQueries, which ignores staleTime, so the
      // UI stays fresh after user actions.
      staleTime: 30_000,
    },
  },
})
