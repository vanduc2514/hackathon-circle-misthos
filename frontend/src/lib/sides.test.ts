import { describe, expect, it } from 'vitest'
import { mayClaim, otherSide, partyOn, sidesOf } from './sides'

const both = { publisher_id: 'PUB-101', contributor_id: 'CON-101' }
const contributor = { publisher_id: null, contributor_id: 'CON-101' }
const publisher = { publisher_id: 'PUB-101', contributor_id: null }

describe('sidesOf', () => {
  it('lists every side a wallet acts on, and none before it chose', () => {
    expect(sidesOf(both)).toEqual(['publisher', 'contributor'])
    expect(sidesOf(contributor)).toEqual(['contributor'])
    expect(sidesOf(null)).toEqual([])
  })
})

describe('otherSide', () => {
  it('offers the side not taken yet, and nothing once it has both', () => {
    expect(otherSide(contributor)).toBe('publisher')
    expect(otherSide(publisher)).toBe('contributor')
    expect(otherSide(both)).toBeNull()
  })
})

describe('partyOn', () => {
  it('names the party for each side', () => {
    expect(partyOn(both, 'publisher')).toBe('PUB-101')
    expect(partyOn(publisher, 'contributor')).toBeNull()
  })
})

describe('mayClaim', () => {
  it('lets a wallet that works on issues claim another publisher’s', () => {
    expect(mayClaim(both, { publisher_id: 'PUB-4' })).toBe(true)
    expect(mayClaim(contributor, { publisher_id: 'PUB-4' })).toBe(true)
  })

  it('never lets it claim its own issue, or claim at all without the contributor side', () => {
    expect(mayClaim(both, { publisher_id: 'PUB-101' })).toBe(false)
    expect(mayClaim(publisher, { publisher_id: 'PUB-4' })).toBe(false)
  })
})
