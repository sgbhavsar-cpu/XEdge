import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, setSessionToken, setUnauthorizedHandler } from '../api/client'
import type { Role } from './permissions'

interface Session {
  username: string
  role: Role
}

interface LoginResponse {
  session_token: string
  username: string
  role: Role
}

interface AuthContextValue {
  session: Session | null
  login: (tenantName: string, username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

// Deliberately in-memory only (useState, no localStorage/sessionStorage) --
// a reload means re-login. Matches FleetSession's own design rationale
// (xedge/fleet/auth.py): a compromised admin session carries user:manage
// over a whole tenant, so it doesn't get the XSS-exfiltratable persistence
// a lower-stakes session might reasonably accept.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)

  // The session is already unusable server-side by the time this fires
  // (that's *why* a request got a 401) -- nothing to revoke, just forget
  // it locally. Distinct from `logout` below, which is the user clicking
  // "log out" on a session that's still live and must be told to die.
  const clearSession = useCallback(() => {
    setSessionToken(null)
    setSession(null)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(clearSession)
  }, [clearSession])

  const logout = useCallback(async () => {
    try {
      await api.post('/api/v1/fleet/auth/logout')
    } finally {
      clearSession()
    }
  }, [clearSession])

  const login = useCallback(async (tenantName: string, username: string, password: string) => {
    const response = await api.post<LoginResponse>('/api/v1/fleet/auth/login', {
      tenant_name: tenantName,
      username,
      password,
    })
    setSessionToken(response.session_token)
    setSession({ username: response.username, role: response.role })
  }, [])

  const value = useMemo(() => ({ session, login, logout }), [session, login, logout])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (context === null) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
