import { describe, expect, it } from 'vitest'
import { money, relativeTime, shortHash } from './client'

describe('money', () => {
  it('formats the decimal view the API returns', () => {
    expect(money({ usdc: '2601.24', base_units: 2601240000 })).toBe('2,601.24')
  })

  it('formats a plain string, which is what the metrics endpoints return', () => {
    expect(money('1473.63')).toBe('1,473.63')
  })

  it('formats a number', () => {
    expect(money(12)).toBe('12.00')
  })

  it('renders an em dash rather than NaN for missing values', () => {
    expect(money(null)).toBe('—')
    expect(money(undefined)).toBe('—')
    expect(money({})).toBe('—')
  })

  it('does not lose the cent on a large amount', () => {
    expect(money({ usdc: '48350.05', base_units: 48350050000 })).toBe('48,350.05')
  })
})

describe('relativeTime', () => {
  it('reads backwards for something that has happened', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 3600_000).toISOString()
    expect(relativeTime(twoHoursAgo)).toBe('2h ago')
  })

  it('reads forwards for a deadline, which is usually in the future', () => {
    const inThreeDays = new Date(Date.now() + 3 * 86_400_000).toISOString()
    expect(relativeTime(inThreeDays)).toBe('in 3d')
  })

  it('never produces a negative value', () => {
    const inTwelveDays = new Date(Date.now() + 12 * 86_400_000).toISOString()
    expect(relativeTime(inTwelveDays)).not.toContain('-')
  })
})

describe('shortHash', () => {
  it('shortens a long hash without hiding both ends', () => {
    const hash = '0x' + 'a'.repeat(62)
    const short = shortHash(hash)
    expect(short).toHaveLength(15)
    expect(short).toContain('…')
    expect(short.startsWith('0xaaaaaa')).toBe(true)
  })

  it('leaves a short value alone', () => {
    expect(shortHash('ISS-1001')).toBe('ISS-1001')
  })
})
