import { describe, expect, it } from 'vitest'
import { MAX_CRITERION, asStored, criteriaLines, sameAsStored, tooLong } from './criteria'

describe('acceptance criteria as the server keeps them', () => {
  it('reads one criterion per non-empty line, trimmed', () => {
    expect(criteriaLines('  a test covers it  \n\n second \n')).toEqual([
      'a test covers it',
      'second',
    ])
  })

  it('keeps only the first 500 characters of a criterion', () => {
    const long = 'x'.repeat(600)
    const stored = asStored([long])
    expect(stored).toHaveLength(MAX_CRITERION)
    expect(tooLong([long])).toBe(true)
  })

  it('settles when the only difference is beyond what the server keeps', () => {
    // The regression: a draft longer than the stored value never compared equal, so
    // the fund button stayed disabled with no explanation.
    const base = 'y'.repeat(MAX_CRITERION)
    const withTail = base + 'and this tail is dropped'
    expect(sameAsStored([withTail], [base])).toBe(true)
    expect(withTail === base).toBe(false)
  })

  it('still notices a real edit', () => {
    expect(sameAsStored(['a test covers it', 'and docs'], ['a test covers it'])).toBe(false)
    expect(sameAsStored(['a test covers it'], ['a test covers it'])).toBe(true)
  })
})
