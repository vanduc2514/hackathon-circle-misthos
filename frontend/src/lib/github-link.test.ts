import { describe, expect, it } from 'vitest'
import { linking } from './github-link'

describe('linking', () => {
  it('never offers "Link with GitHub" where no OAuth App is set up, the state of a fresh install', () => {
    expect(linking({ simulated: true, github_oauth: false }).oauth).toBe(false)
    expect(linking({ simulated: false, github_oauth: false }).oauth).toBe(false)
  })

  it('makes a typed login the way to link in the simulation without one, and says why', () => {
    expect(linking({ simulated: true, github_oauth: false })).toEqual({
      oauth: false,
      byLogin: 'main',
      notSetUp: 'simulation',
    })
  })

  it('tells a deployment without one what its operator must set up, and offers nothing', () => {
    expect(linking({ simulated: false, github_oauth: false })).toEqual({
      oauth: false,
      byLogin: null,
      notSetUp: 'deployment',
    })
  })

  it('offers GitHub where it is set up, and a typed login only beside it in the simulation', () => {
    expect(linking({ simulated: false, github_oauth: true })).toEqual({
      oauth: true,
      byLogin: null,
      notSetUp: null,
    })
    expect(linking({ simulated: true, github_oauth: true })).toEqual({
      oauth: true,
      byLogin: 'other',
      notSetUp: null,
    })
  })
})
