// Bridges Clerk's React-only getToken() into the module level axios singleton.
// App.tsx registers the getter once on mount; the axios interceptor calls it.

type TokenGetter = () => Promise<string | null>

let tokenGetter: TokenGetter | null = null

export function setTokenGetter(getter: TokenGetter) {
  tokenGetter = getter
}

export async function getAuthToken(): Promise<string | null> {
  if (!tokenGetter) return null
  return tokenGetter()
}
