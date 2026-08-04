import { describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/renderWithProviders'
import UsersPage from './UsersPage'

const { getMock, postMock, patchMock, deleteMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  patchMock: vi.fn(),
  deleteMock: vi.fn(),
}))
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, get: getMock, post: postMock, patch: patchMock, delete: deleteMock } }
})

const USERS = [
  { username: 'admin', role: 'admin' as const },
  { username: 'alice', role: 'readonly' as const },
]

describe('UsersPage', () => {
  it('lists existing accounts with a role picker per row', async () => {
    getMock.mockResolvedValue(USERS)
    renderWithProviders(<UsersPage />)

    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument())
    expect(screen.getByRole('cell', { name: 'admin' })).toBeInTheDocument()
  })

  it('creates a user via the form', async () => {
    getMock.mockResolvedValue([])
    postMock.mockResolvedValueOnce({ username: 'bob', role: 'operator' })
    renderWithProviders(<UsersPage />)

    await userEvent.type(screen.getByLabelText(/^Username/), 'bob')
    await userEvent.type(screen.getByLabelText(/^Password/), 'hunter2hunter2')
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith('/api/v1/fleet/users', {
        username: 'bob',
        password: 'hunter2hunter2',
        role: 'readonly',
      }),
    )
  })

  it('deletes a user and surfaces a 409 (last-admin) error', async () => {
    const { ApiError } = await vi.importActual<typeof import('../api/client')>('../api/client')
    getMock.mockResolvedValue(USERS)
    deleteMock.mockRejectedValueOnce(new ApiError(409, "cannot delete the tenant's last admin"))
    renderWithProviders(<UsersPage />)

    await waitFor(() => expect(screen.getByRole('cell', { name: 'admin' })).toBeInTheDocument())
    const deleteButtons = screen.getAllByRole('button', { name: /delete/i })
    await userEvent.click(deleteButtons[0])

    await waitFor(() => expect(screen.getByText(/last admin/)).toBeInTheDocument())
  })
})
