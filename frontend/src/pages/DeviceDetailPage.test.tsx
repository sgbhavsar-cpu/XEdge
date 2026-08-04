import { useEffect, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/renderWithProviders'
import { useAuth } from '../auth/AuthContext'
import type { CertificateHistoryEntry, ConfigHistoryEntry, DeviceSummary } from '../api/types'
import DeviceDetailPage from './DeviceDetailPage'

const { getMock, patchMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  patchMock: vi.fn(),
  postMock: vi.fn(),
}))
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, get: getMock, patch: patchMock, post: postMock } }
})

const DEVICE: DeviceSummary = {
  device_id: 'demo-gw-1',
  display_name: 'Line 1 Gateway',
  status: 'online',
  connection_state: 'active',
  registered_at: '2026-01-01T00:00:00Z',
  agent_version: '1.2.3',
  last_seen_at: '2026-01-02T00:00:00Z',
  driver_count: 2,
  uptime_seconds: 3600,
  last_config_apply: { version: 3, success: true, error: null },
  has_pending_config: false,
  pending_config_version: 3,
  cert_serial_number: 'AA:BB',
  cert_not_after: new Date(Date.now() + 90 * 24 * 60 * 60 * 1000).toISOString(),
  serial_number: 'SN-1',
  make: 'Acme',
  protocol: 'modbus',
  hardware_firmware_version: '1.0',
}

const CONFIG_HISTORY: ConfigHistoryEntry[] = [
  {
    config_version: 3,
    config: {},
    pushed_at: '2026-01-01T00:00:00Z',
    pushed_by: 'admin',
    applied_at: '2026-01-01T00:01:00Z',
    apply_success: true,
    apply_error: null,
  },
]

const CERT_HISTORY: CertificateHistoryEntry[] = [
  {
    serial_number: 'AA:BB',
    not_before: '2026-01-01T00:00:00Z',
    not_after: '2026-04-01T00:00:00Z',
    issued_at: '2026-01-01T00:00:00Z',
    reason: 'enrollment',
  },
]

function mockEndpoints() {
  getMock.mockImplementation((path: string) => {
    if (path.endsWith('/config-history')) return Promise.resolve(CONFIG_HISTORY)
    if (path.endsWith('/certificate-history')) return Promise.resolve(CERT_HISTORY)
    return Promise.resolve(DEVICE)
  })
  postMock.mockResolvedValue({ session_token: 'tok', username: 'admin', role: 'admin' })
}

// Every write-gated action on this page (and PR 6/7's, later) needs a real
// session in context -- AuthProvider only ever gets one via login(), so
// this drives that instead of reaching into AuthContext's internals.
function LoggedInDeviceDetail() {
  const { login } = useAuth()
  const [ready, setReady] = useState(false)
  useEffect(() => {
    void login('default', 'admin', 'pw').then(() => setReady(true))
  }, [login])
  if (!ready) return null
  return (
    <Routes>
      <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
    </Routes>
  )
}

function renderPage() {
  return renderWithProviders(<LoggedInDeviceDetail />, { route: '/devices/demo-gw-1' })
}

describe('DeviceDetailPage', () => {
  it('renders device header, metadata, pending config, and history tables', async () => {
    mockEndpoints()
    renderPage()

    await waitFor(() => expect(screen.getByText('Line 1 Gateway')).toBeInTheDocument())
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Acme')).toBeInTheDocument()
    expect(screen.getByText('None')).toBeInTheDocument() // has_pending_config: false
    expect(screen.getByText(/succeeded/)).toBeInTheDocument() // last_config_apply
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument()) // config history row
    expect(screen.getByText('enrollment')).toBeInTheDocument() // cert history row
  })

  it('saves metadata edits via PATCH and shows the updated value', async () => {
    mockEndpoints()
    patchMock.mockResolvedValueOnce({ ...DEVICE, make: 'Updated Make' })
    renderPage()

    await waitFor(() => expect(screen.getByDisplayValue('Acme')).toBeInTheDocument())
    const makeField = screen.getByLabelText('Make')
    await userEvent.clear(makeField)
    await userEvent.type(makeField, 'Updated Make')
    await userEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument())
    expect(patchMock).toHaveBeenCalledWith(
      '/api/v1/fleet/devices/demo-gw-1/metadata',
      expect.objectContaining({ make: 'Updated Make' }),
    )
  })
})
