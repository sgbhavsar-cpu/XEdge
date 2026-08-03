import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '../test/renderWithProviders'
import LoginPage from './LoginPage'

describe('LoginPage', () => {
  it('renders the tenant, username, and password fields', () => {
    renderWithProviders(<LoginPage />)

    // Regex, not an exact string: MUI's required-field indicator appends
    // a visually-hidden " *" to the label's text content, which
    // getByLabelText's default exact matcher would otherwise fail on.
    expect(screen.getByLabelText(/^Tenant/)).toBeInTheDocument()
    expect(screen.getByLabelText(/^Username/)).toBeInTheDocument()
    expect(screen.getByLabelText(/^Password/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument()
  })

  it('defaults the tenant field to "default"', () => {
    renderWithProviders(<LoginPage />)

    expect(screen.getByLabelText(/^Tenant/)).toHaveValue('default')
  })
})
