// Typed wrapper over the Fleet Manager's REST API (xedge/fleet/manager_app.py).
// Relative paths only -- in production this is served by the same origin
// as the API (ADR-013 §6), and in dev the Vite proxy forwards
// /api/v1/fleet/* to the manager's admin port (see vite.config.ts).

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

let sessionToken: string | null = null
let onUnauthorized: (() => void) | null = null

// Called once by AuthProvider at mount -- the api client is a plain
// module with no React context of its own, so this is the seam that lets
// a 401 from *any* call trigger the same "forget the session, go to
// /login" path a manual logout already uses.
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler
}

export function setSessionToken(token: string | null): void {
  sessionToken = token
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Content-Type', 'application/json')
  if (sessionToken !== null) {
    headers.set('Authorization', `Bearer ${sessionToken}`)
  }

  const response = await fetch(path, { ...init, headers })

  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = typeof body?.detail === 'string' ? body.detail : response.statusText
    // A 401 on the login call itself just means "wrong credentials" --
    // there's no session for onUnauthorized() to clear (sessionToken is
    // already null), so this is a harmless no-op there. Anywhere else,
    // it means the *existing* session died server-side (idle timeout,
    // an admin deleting the account, ...), which is exactly what should
    // trigger the same "forget it, go to /login" path a manual logout
    // uses. Either way, always surface the backend's real error message
    // -- a hardcoded one here would bury *why* login failed.
    if (response.status === 401) {
      onUnauthorized?.()
    }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
