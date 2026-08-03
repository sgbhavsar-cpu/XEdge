import type { ConnectionState } from '../api/types'

// Mirrors xedge/api/static/xedge-ui.js's formatCertExpiry (XEDGE-447) --
// same threshold and wording, reimplemented here since the dashboard has
// no shared JS runtime with the device-local Web UI.
export const CERT_EXPIRY_WARNING_DAYS = 14

export function formatCertExpiry(notAfter: string | null): string {
  if (!notAfter) return 'not enrolled'
  const expiry = new Date(notAfter)
  const daysRemaining = (expiry.getTime() - Date.now()) / (1000 * 60 * 60 * 24)
  const formatted = expiry.toLocaleDateString()
  if (daysRemaining < 0) return `${formatted} (expired)`
  if (daysRemaining < CERT_EXPIRY_WARNING_DAYS) {
    return `${formatted} (expires in ${Math.floor(daysRemaining)}d)`
  }
  return formatted
}

export function formatLastSeen(lastSeenAt: string | null): string {
  return lastSeenAt ? new Date(lastSeenAt).toLocaleString() : 'never'
}

// Chip colors for GatewayConnectionState (xedge/fleet/registry.py) --
// success/info/error map onto the CRD's "healthy vs. degraded vs. down"
// intent; "inactive" (never enrolled/heartbeated) gets neutral default
// rather than error, since it isn't a failure of a device that once worked.
const CONNECTION_STATE_COLOR: Record<ConnectionState, 'success' | 'info' | 'error' | 'default'> = {
  active: 'success',
  connected: 'info',
  disconnected: 'error',
  inactive: 'default',
}

export function connectionStateColor(state: ConnectionState): 'success' | 'info' | 'error' | 'default' {
  return CONNECTION_STATE_COLOR[state]
}
