import { describe, expect, it } from 'vitest'
import { supportStatus, type SupportRequest } from './support'

function request(over: Partial<SupportRequest> = {}): SupportRequest {
  return {
    id: 'SUP-1',
    publisher_id: 'PUB-104',
    severity: 'urgent',
    subject: 'Payout held',
    body: 'x',
    opened_by: 'dana@acme.example',
    opened_at: '2026-10-09T15:00:00Z',
    respond_by: '2026-10-09T19:00:00Z',
    overdue: false,
    ...over,
  }
}

describe('supportStatus', () => {
  it('says when an open request is due, in UTC', () => {
    expect(supportStatus(request())).toBe('First response due Fri, 09 Oct 2026 19:00 UTC')
  })

  it('says plainly when the deadline was missed, answered or not', () => {
    expect(supportStatus(request({ overdue: true }))).toBe(
      'Late: a first response was due Fri, 09 Oct 2026 19:00 UTC',
    )
    expect(
      supportStatus(request({ overdue: true, first_response_at: '2026-10-09T21:00:00Z' })),
    ).toBe('Answered late (due Fri, 09 Oct 2026 19:00 UTC)')
  })

  it('says an answer in time is in time', () => {
    expect(supportStatus(request({ first_response_at: '2026-10-09T16:00:00Z' }))).toBe(
      'Answered in time',
    )
  })
})
