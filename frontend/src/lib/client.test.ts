import { describe, expect, it } from 'vitest'
import { ApiError, checksChip, money, monthName, relativeTime, shortHash, unwrap } from './client'

describe('money', () => {
  it('formats the decimal view the API returns', () => {
    expect(money({ usdc: '2601.24', base_units: 2601240000 })).toBe('2,601.24')
  })

  it('formats a plain string, which is what the metrics endpoints return', () => {
    expect(money('1473.63')).toBe('1,473.63')
  })

  it('reads a figure that arrives already grouped', () => {
    expect(money('48,500.00')).toBe('48,500.00')
  })

  it('renders an em dash rather than NaN for a string that is not a number', () => {
    expect(money('n/a')).toBe('—')
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

describe('monthName', () => {
  it('names the month the server counted', () => {
    expect(monthName('2026-10')).toBe('October 2026')
  })

  it('reads the month in UTC, so the first of the month is not the month before', () => {
    expect(monthName('2026-01')).toBe('January 2026')
  })

  it('shows anything that is not a year and month as it came', () => {
    expect(monthName('soon')).toBe('soon')
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

describe('unwrap', () => {
  const response = (status: number) => new Response(null, { status, statusText: 'Conflict' })

  it('returns the body of a successful call', () => {
    expect(unwrap({ data: { id: 'ISS-1' }, response: response(200) })).toEqual({ id: 'ISS-1' })
  })

  it('throws the reason the API gave', () => {
    const call = () =>
      unwrap({ error: { detail: 'fund after approving the criteria' }, response: response(409) })
    expect(call).toThrow(ApiError)
    expect(call).toThrow('fund after approving the criteria')
  })

  it('joins validation errors into one sentence', () => {
    const error = { detail: [{ msg: 'field required' }, { msg: 'too long' }] }
    expect(() => unwrap({ error, response: response(422) })).toThrow('field required; too long')
  })

  const refusal = (error: unknown): ApiError => {
    try {
      unwrap({ error, response: response(409) })
    } catch (thrown) {
      if (thrown instanceof ApiError) return thrown
    }
    throw new Error('unwrap did not refuse')
  }

  it('keeps the issue a second publish of an open GitHub issue names, to link to it', () => {
    const refused = refusal({
      detail: 'acme/widgets#5 is already on Misthos as ISS-1009',
      issue_id: 'ISS-1009',
    })
    expect(refused.issueId).toBe('ISS-1009')
    expect(refused.message).toBe('acme/widgets#5 is already on Misthos as ISS-1009')
  })

  it('names no issue when the refusal does not', () => {
    expect(refusal({ detail: 'another action is in progress' }).issueId).toBeNull()
  })
})

describe('checksChip', () => {
  it('says checks that have not reported are not reported, not failing', () => {
    expect(checksChip(null)).toEqual({ label: 'not reported', tone: 'warn' })
  })

  it('says passing and failing checks as they are', () => {
    expect(checksChip(true)).toEqual({ label: 'passing', tone: 'ok' })
    expect(checksChip(false)).toEqual({ label: 'failing', tone: 'bad' })
  })
})
