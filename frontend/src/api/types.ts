// Mirrors xedge/fleet/manager_app.py's _device_summary() JSON shape --
// keep in sync with that function, not with DeviceRecord (registry.py),
// since the wire format is what this file documents.
export type ConnectionState = 'inactive' | 'active' | 'connected' | 'disconnected'

// xedge/fleet/agent.py's _apply_pending_config() return shape, round-tripped
// through Device.last_config_apply_json unchanged.
export interface ConfigApplyReport {
  version: number
  success: boolean
  error: string | null
}

export interface DeviceSummary {
  device_id: string
  display_name: string | null
  status: string
  connection_state: ConnectionState
  registered_at: string
  agent_version: string | null
  last_seen_at: string | null
  driver_count: number | null
  uptime_seconds: number | null
  last_config_apply: ConfigApplyReport | null
  has_pending_config: boolean
  pending_config_version: number
  cert_serial_number: string | null
  cert_not_after: string | null
  serial_number: string | null
  make: string | null
  protocol: string | null
  hardware_firmware_version: string | null
}

// GET /api/v1/fleet/devices/{device_id}/config-history (XEDGE-512).
export interface ConfigHistoryEntry {
  config_version: number
  config: Record<string, unknown>
  pushed_at: string
  pushed_by: string
  applied_at: string | null
  apply_success: boolean | null
  apply_error: string | null
}

// GET /api/v1/fleet/devices/{device_id}/certificate-history (XEDGE-512).
export interface CertificateHistoryEntry {
  serial_number: string
  not_before: string
  not_after: string
  issued_at: string
  reason: string
}

// GET/POST/DELETE /api/v1/fleet/join-tokens (XEDGE-513).
export type JoinTokenStatus = 'active' | 'consumed' | 'revoked' | 'expired'

export interface JoinTokenRecord {
  id: string
  device_id: string
  created_at: string
  expires_at: string
  consumed_at: string | null
  revoked_at: string | null
  revoked_by: string | null
  status: JoinTokenStatus
}

export interface CreateJoinTokenResponse {
  join_token: string
  device_id: string
  ttl_seconds: number
}

// PATCH /api/v1/fleet/devices/{device_id}/metadata -- every field optional
// and omitted-vs-null-aware server-side (see _UpdateMetadataBody's
// docstring), but the frontend always sends every field it shows, so
// "leave unchanged" here just means "send the current value back".
export interface DeviceMetadataUpdate {
  display_name?: string | null
  serial_number?: string | null
  make?: string | null
  protocol?: string | null
  hardware_firmware_version?: string | null
}
