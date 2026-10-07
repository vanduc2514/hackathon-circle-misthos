import { useQuery } from '@tanstack/react-query'
import { api, unwrap, type MeOut } from './client'
import { siweMessage } from './siwe'
import type { Signer } from './wallet'

/** The API's health, which also says whether this is the simulation. */
export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: async () => unwrap(await api.GET('/api/v1/health')),
  })
}

/** Who is signed in: the wallet and its account, or null when nobody is. */
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
 * Sign in: the API issues a nonce and says what the message must name, the wallet
 * signs it, and the API checks it and sets the session cookie.
 */
export async function signIn(signer: Signer): Promise<void> {
  const terms = unwrap(await api.POST('/api/v1/auth/nonce'))
  const message = siweMessage(terms, signer.address, new Date())
  const signature = await signer.sign(message)
  unwrap(await api.POST('/api/v1/auth/verify', { body: { message, signature } }))
}

export async function signOut(): Promise<void> {
  await api.POST('/api/v1/auth/logout')
}
