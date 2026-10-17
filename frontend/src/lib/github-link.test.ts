import { describe, expect, it } from 'vitest'
import { linking } from './github-link'

describe('linking', () => {
  it('never offers "Link with GitHub" where no OAuth App is set up', () => {
    expect(linking({ github_oauth: false })).toEqual({
      oauth: false,
      notSetUp: 'deployment',
    })
  })

  it('offers GitHub when the server has an OAuth App', () => {
    expect(linking({ github_oauth: true })).toEqual({ oauth: true, notSetUp: null })
  })
})
