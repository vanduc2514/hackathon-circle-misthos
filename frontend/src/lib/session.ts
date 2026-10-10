import { useQuery } from '@tanstack/react-query'
import { api, unwrap, type Account, type MeOut } from './client'
import { siweMessage } from './siwe'
import type { Signer } from './wallet'

/** The API's health, which also says whether this is the simulation. */
export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: async () => unwrap(await api.GET('/api/v1/health')),
  })
}

/**
 * Who is signed in, how (GitHub, a wallet or single sign-on), and their account; null
 * when nobody is.
 */
export function useMe() {
  return useQuery({
    queryKey: ['me'],
    retry: false,
    queryFn: async (): Promise<MeOut | null> => {
      const result = await api.GET('/api/v1/auth/me')
      if (result.response.status === 401) return null
      return unwrap(result)
    },
  })
}

/**
 * A message proving the signer holds its wallet: the API issues a one-time nonce and
 * says what the message must name, and the wallet signs it. Signing in and connecting
 * a wallet both take one, and each nonce is good for one of them.
 */
async function proof(signer: Signer): Promise<{ message: string; signature: string }> {
  const terms = unwrap(await api.POST('/api/v1/auth/nonce'))
  const message = siweMessage(terms, signer.address, new Date())
  return { message, signature: await signer.sign(message) }
}

/** Sign in with a wallet: the API checks the proof and sets the session cookie. */
export async function signIn(signer: Signer): Promise<void> {
  unwrap(await api.POST('/api/v1/auth/verify', { body: await proof(signer) }))
}

/**
 * Sign in with GitHub: the API sets a short-lived cookie holding the state and answers
 * with GitHub's address, and GitHub sends the browser back to /account?signed_in=github.
 */
export async function signInWithGitHub(): Promise<void> {
  const { authorize_url } = unwrap(await api.POST('/api/v1/auth/github/signin'))
  window.location.assign(authorize_url)
}

/** The simulation's GitHub sign-in: a typed login stands in for OAuth. */
export async function demoGitHubSignIn(login: string): Promise<void> {
  unwrap(await api.POST('/api/v1/auth/github/simulate-signin', { body: { login } }))
}

/**
 * Sign in through an organisation's single sign-on (#53): the work e-mail's domain
 * finds its identity provider, the API sets a short-lived cookie holding the state, and
 * the provider sends the browser back to /account?signed_in=sso.
 */
export async function signInWithSso(email: string): Promise<void> {
  const { authorize_url } = unwrap(await api.POST('/api/v1/auth/sso/signin', { body: { email } }))
  window.location.assign(authorize_url)
}

/** The simulation's single sign-on: a typed work e-mail stands in for the provider. */
export async function demoSsoSignIn(email: string): Promise<void> {
  unwrap(await api.POST('/api/v1/auth/sso/simulate-signin', { body: { email } }))
}

/**
 * Connect a wallet to the signed-in account, so it can fund or be paid (#131). Before a
 * side is chosen, the wallet of an account from before GitHub sign-in joins it instead.
 */
export async function connectWallet(signer: Signer): Promise<Account> {
  return unwrap(await api.POST('/api/v1/auth/wallet/connect', { body: await proof(signer) }))
}

export async function signOut(): Promise<void> {
  await api.POST('/api/v1/auth/logout')
}
