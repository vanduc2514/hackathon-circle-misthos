/**
 * How the sign-in gate offers to link a GitHub account (#118, #181).
 *
 * "Link with GitHub" is offered only where the server has a GitHub OAuth App, because
 * anywhere else it can only fail. A deployment without one says what its operator has
 * to set up, since nothing can be linked until then.
 */
export type Linking = {
  /** Offer "Link with GitHub", which goes to GitHub and back. */
  oauth: boolean
  /** Why "Link with GitHub" is not offered, when it is not. */
  notSetUp: 'deployment' | null
}

export function linking(health: { github_oauth: boolean }): Linking {
  return health.github_oauth
    ? { oauth: true, notSetUp: null }
    : { oauth: false, notSetUp: 'deployment' }
}
