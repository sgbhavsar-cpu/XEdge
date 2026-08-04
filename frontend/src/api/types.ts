// Mirrors xedge/fleet/manager_app.py's _device_summary() JSON shape --
// keep in sync with that function, not with DeviceRecord (registry.py),
// since the wire format is what this file documents.
export type ConnectionState = 'inactive' | 'active' | 'connected' | 'disconnected'

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
  last_config_apply: Record<string, unknown> | null
  has_pending_config: boolean
  pending_config_version: number
  cert_serial_number: string | null
  cert_not_after: string | null
  serial_number: string | null
  make: string | null
  protocol: string | null
  hardware_firmware_version: string | null
}
