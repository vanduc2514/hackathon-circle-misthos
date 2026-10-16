import { describe, expect, it } from 'vitest'
import { deadlineFor, termNote, termProblem } from './term'

describe('termProblem', () => {
  it('accepts a whole number of days within the bounds, the bounds included', () => {
    expect(termProblem('3')).toBeNull()
    expect(termProblem('14')).toBeNull()
    expect(termProblem(' 90 ')).toBeNull()
  })

  it('says why a term would be refused', () => {
    expect(termProblem('2')).toBe('Between 3 and 90 days.')
    expect(termProblem('91')).toBe('Between 3 and 90 days.')
    expect(termProblem('0')).toBe('Between 3 and 90 days.')
    expect(termProblem('7.5')).toBe('Name the term in whole days.')
    expect(termProblem('a week')).toBe('Name the term as a number of days.')
    expect(termProblem('')).toBe('Name the term as a number of days.')
  })
})

describe('deadlineFor', () => {
  it('adds whole days to the moment the money is committed', () => {
    const from = new Date('2026-10-15T09:30:00Z')
    expect(deadlineFor(14, from).toISOString()).toBe('2026-10-29T09:30:00.000Z')
  })
})

describe('termNote', () => {
  it('tells a contributor the term before the money is committed, and the date after', () => {
    expect(termNote(30, null)).toBe('30 days once funded')
    expect(termNote(14, '2026-10-29T09:30:00Z')).toBe('14 days, until 29 October 2026')
  })
})
