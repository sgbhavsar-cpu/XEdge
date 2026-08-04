import { describe, expect, it } from 'vitest'
import { connectionStateColor, formatCertExpiry, formatLastSeen } from './deviceFormatting'

describe('formatCertExpiry', () => {
  it('reports "not enrolled" for a null cert', () => {
    expect(formatCertExpiry(null)).toBe('not enrolled')
  })

  it('reports "expired" for a past date', () => {
    const past = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString()
    expect(formatCertExpiry(past)).toContain('(expired)')
  })

  it('reports days remaining inside the warning window', () => {
    const soon = new Date(Date.now() + 5 * 24 * 60 * 60 * 1000).toISOString()
    expect(formatCertExpiry(soon)).toMatch(/\(expires in \dd\)/)
  })

  it('reports only the date outside the warning window', () => {
    const later = new Date(Date.now() + 60 * 24 * 60 * 60 * 1000).toISOString()
    expect(formatCertExpiry(later)).not.toContain('expires in')
    expect(formatCertExpiry(later)).not.toContain('expired')
  })
})

describe('formatLastSeen', () => {
  it('reports "never" for a null timestamp', () => {
    expect(formatLastSeen(null)).toBe('never')
  })

  it('formats a real timestamp', () => {
    expect(formatLastSeen('2026-01-01T00:00:00Z')).not.toBe('never')
  })
})

describe('connectionStateColor', () => {
  it('maps every GatewayConnectionState value to a Chip color', () => {
    expect(connectionStateColor('active')).toBe('success')
    expect(connectionStateColor('connected')).toBe('info')
    expect(connectionStateColor('disconnected')).toBe('error')
    expect(connectionStateColor('inactive')).toBe('default')
  })
})
