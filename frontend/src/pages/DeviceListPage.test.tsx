import { describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/renderWithProviders'
import type { DeviceSummary } from '../api/types'
import DeviceListPage from './DeviceListPage'

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }))
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, get: getMock } }
})

function device(overrides: Partial<DeviceSummary>): DeviceSummary {
  return {
    device_id: 'dev-1',
    display_name: 'Line 1 Gateway',
    status: 'online',
    connection_state: 'active',
    registered_at: '2026-01-01T00:00:00Z',
    agent_version: '1.2.3',
    last_seen_at: '2026-01-02T00:00:00Z',
    driver_count: 2,
    uptime_seconds: 3600,
    last_config_apply: null,
    has_pending_config: false,
    pending_config_version: 0,
    cert_serial_number: 'AA:BB',
    cert_not_after: new Date(Date.now() + 90 * 24 * 60 * 60 * 1000).toISOString(),
    serial_number: 'SN-1',
    make: 'Acme',
    protocol: 'modbus',
    hardware_firmware_version: '1.0',
    ...overrides,
  }
}

describe('DeviceListPage', () => {
  it('renders a row per device with its connection state and cert expiry', async () => {
    getMock.mockResolvedValueOnce([
      device({ device_id: 'dev-1', display_name: 'Line 1 Gateway', connection_state: 'active' }),
      device({
        device_id: 'dev-2',
        display_name: null,
        connection_state: 'disconnected',
        cert_not_after: null,
      }),
    ])

    renderWithProviders(<DeviceListPage />)

    await waitFor(() => expect(screen.getByText('Line 1 Gateway')).toBeInTheDocument())
    // Falls back to device_id when display_name is null -- appears twice,
    // once for the Name column's fallback and once for the Device ID column.
    expect(screen.getAllByText('dev-2')).toHaveLength(2)
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('disconnected')).toBeInTheDocument()
    expect(screen.getByText('not enrolled')).toBeInTheDocument()
  })

  it('shows an error alert when the request fails', async () => {
    const { ApiError } = await vi.importActual<typeof import('../api/client')>('../api/client')
    getMock.mockRejectedValueOnce(new ApiError(500, 'boom'))

    renderWithProviders(<DeviceListPage />)

    await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument())
  })
})
