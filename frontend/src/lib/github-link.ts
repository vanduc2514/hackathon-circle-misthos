/**
 * How the Account page offers to link a GitHub account (#118).
 *
 * "Link with GitHub" is offered only where the server has a GitHub OAuth App, because
 * anywhere else it can only fail. In the simulation without one, typing a login is the
 * way to link, not a form under a button that does not work; and a deployment without
 * one says what its operator has to set up, since nothing can be linked until then.
 */
export type Linking = {
  /** Offer "Link with GitHub", which goes to GitHub and back. */
  oauth: boolean
  /** Linking by a typed login, the simulation's own: the way to link, or the other way. */
  byLogin: 'main' | 'other' | null
  /** Why "Link with GitHub" is not offered, when it is not. */
  notSetUp: 'simulation' | 'deployment' | null
}

export function linking(health: { simulated: boolean; github_oauth: boolean }): Linking {
  if (health.github_oauth) {
    return { oauth: true, byLogin: health.simulated ? 'other' : null, notSetUp: null }
  }
  return health.simulated
    ? { oauth: false, byLogin: 'main', notSetUp: 'simulation' }
    : { oauth: false, byLogin: null, notSetUp: 'deployment' }
}
