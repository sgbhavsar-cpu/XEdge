import { describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/renderWithProviders'
import type { JoinTokenRecord } from '../api/types'
import JoinTokensPage from './JoinTokensPage'

const { getMock, postMock, deleteMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  deleteMock: vi.fn(),
}))
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, get: getMock, post: postMock, delete: deleteMock } }
})

const ACTIVE_TOKEN: JoinTokenRecord = {
  id: 'hash-1',
  device_id: 'demo-gw-1',
  created_at: '2026-01-01T00:00:00Z',
  expires_at: '2026-01-01T01:00:00Z',
  consumed_at: null,
  revoked_at: null,
  revoked_by: null,
  status: 'active',
}

describe('JoinTokensPage', () => {
  it('lists issued tokens with a revoke action for active ones', async () => {
    getMock.mockResolvedValue([ACTIVE_TOKEN])
    renderWithProviders(<JoinTokensPage />)

    await waitFor(() => expect(screen.getByText('demo-gw-1')).toBeInTheDocument())
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /revoke/i })).toBeInTheDocument()
  })

  it('shows an empty state when there are no tokens', async () => {
    getMock.mockResolvedValue([])
    renderWithProviders(<JoinTokensPage />)

    await waitFor(() => expect(screen.getByText('No join tokens issued yet.')).toBeInTheDocument())
  })

  it('issues a token and shows it once in a dialog', async () => {
    getMock.mockResolvedValue([])
    postMock.mockResolvedValueOnce({ join_token: 'raw-token-abc', device_id: 'new-device', ttl_seconds: 3600 })
    renderWithProviders(<JoinTokensPage />)

    await userEvent.type(screen.getByLabelText(/^Device ID/), 'new-device')
    await userEvent.click(screen.getByRole('button', { name: /issue token/i }))

    await waitFor(() => expect(screen.getByText('Join token issued')).toBeInTheDocument())
    expect(screen.getByDisplayValue('raw-token-abc')).toBeInTheDocument()
    expect(postMock).toHaveBeenCalledWith('/api/v1/fleet/join-tokens', { device_id: 'new-device' })
  })

  it('revokes an active token', async () => {
    getMock.mockResolvedValue([ACTIVE_TOKEN])
    deleteMock.mockResolvedValueOnce(undefined)
    renderWithProviders(<JoinTokensPage />)

    await waitFor(() => expect(screen.getByRole('button', { name: /revoke/i })).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /revoke/i }))

    await waitFor(() =>
      expect(deleteMock).toHaveBeenCalledWith('/api/v1/fleet/join-tokens/hash-1'),
    )
  })
})
