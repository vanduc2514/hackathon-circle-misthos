/**
 * Set up a party's own Circle wallet, where its payouts land (#29).
 *
 * The wallet is user-controlled: the person sets a PIN in Circle's hosted frame and
 * holds their share of a 2-of-2 key. The API starts a session and, when the party has
 * no Arc wallet yet, returns a challenge; this runs that challenge in the browser, and
 * the API then reads the new address back from Circle. Neither the platform nor this
 * page ever sees the PIN or a key.
 */

import { api, unwrap } from './client'
import { toChecksumAddress } from './siwe'
import type { Signer } from './wallet'

export interface WalletSession {
  app_id: string
  user_token: string
  encryption_key: string
  challenge_id: string | null
  simulated: boolean
}

/** The part of Circle's web SDK this uses, so tests can stand in for the frame. */
export interface ChallengeRunner {
  run: (session: WalletSession & { challenge_id: string }) => Promise<void>
}

/** What finishing set-up took, for the page to say. */
export type SetupOutcome = 'linked' | 'created'

async function circleRunner(): Promise<ChallengeRunner> {
  // Loaded on click: the SDK brings Circle's frame and its dependencies, which no
  // other page needs.
  const { W3SSdk } = await import('@circle-fin/w3s-pw-web-sdk')
  return {
    run: async (session) => {
      const sdk = new W3SSdk({ appSettings: { appId: session.app_id } })
      // Without the device id the frame has no session and `execute` silently does
      // nothing, so it is fetched before anything else.
      await sdk.getDeviceId()
      sdk.setAuthentication({
        userToken: session.user_token,
        encryptionKey: session.encryption_key,
      })
      await new Promise<void>((resolve, reject) => {
        sdk.execute(session.challenge_id, (error, result) => {
          if (error) {
            reject(new Error(error.message || 'The wallet set-up was not completed'))
            return
          }
          const status = (result as { status?: string } | undefined)?.status
          if (status && status !== 'COMPLETE' && status !== 'IN_PROGRESS') {
            reject(new Error(`The wallet set-up ended as ${status.toLowerCase()}`))
            return
          }
          resolve()
        })
      })
    },
  }
}

/**
 * Start a session; run the PIN challenge if the wallet does not exist yet; then have
 * the API read the address back and keep it. An existing wallet is linked by the
 * session itself, and the simulation never opens a frame.
 */
export async function setUpWallet(
  start: () => Promise<WalletSession & { wallet: unknown }>,
  link: () => Promise<unknown>,
  runner?: ChallengeRunner,
): Promise<SetupOutcome> {
  const session = await start()
  if (session.wallet || session.simulated || !session.challenge_id) return 'linked'
  const frame = runner ?? (await circleRunner())
  await frame.run({ ...session, challenge_id: session.challenge_id })
  await link()
  return 'created'
}

export interface CircleSignRunner {
  runSign: (session: WalletSession & { challenge_id: string }) => Promise<string | null>
}

async function circleSignRunner(): Promise<CircleSignRunner> {
  const { W3SSdk } = await import('@circle-fin/w3s-pw-web-sdk')
  return {
    runSign: async (session) => {
      const sdk = new W3SSdk({ appSettings: { appId: session.app_id } })
      await sdk.getDeviceId()
      sdk.setAuthentication({
        userToken: session.user_token,
        encryptionKey: session.encryption_key,
      })
      return new Promise<string | null>((resolve, reject) => {
        sdk.execute(session.challenge_id, (error, result) => {
          if (error) {
            reject(new Error(error.message || 'Signing was not completed'))
            return
          }
          const signRes = result as { type?: string; data?: { signature?: string } } | undefined
          if (signRes?.data?.signature) {
            resolve(signRes.data.signature)
            return
          }
          resolve(null)
        })
      })
    },
  }
}

/**
 * A Signer powered by a user-controlled Circle wallet (#181).
 * Obtains or resumes an identity session, runs the PIN challenge if a wallet needs
 * to be created, and answers message signing challenges via ChallengeType.SIGN_MESSAGE.
 */
export async function circleWallet(
  runner?: ChallengeRunner,
  signRunner?: CircleSignRunner,
): Promise<Signer> {
  let userId: string | null = null
  try {
    userId = localStorage.getItem('misthos.circle-wallet.user-id')
  } catch {
    // Storage can be blocked in private mode
  }
  const sessionRes = unwrap(
    await api.POST('/api/v1/wallets/circle/session', {
      body: userId ? { user_id: userId } : {},
    }),
  )
  if (sessionRes.circle_user_id) {
    try {
      localStorage.setItem('misthos.circle-wallet.user-id', sessionRes.circle_user_id)
    } catch {
      // Storage can be blocked
    }
  }

  // If there is a PIN challenge to create the wallet first:
  if (!sessionRes.simulated && sessionRes.challenge_id && !sessionRes.wallet?.address) {
    const frame = runner ?? (await circleRunner())
    await frame.run({ ...sessionRes, challenge_id: sessionRes.challenge_id })
  }

  const address = sessionRes.wallet?.address
  if (!address) {
    throw new Error('Circle wallet has no address')
  }

  return {
    kind: 'circle',
    address: toChecksumAddress(address),
    sign: async (message: string) => {
      const signRes = unwrap(
        await api.POST('/api/v1/wallets/circle/sign', {
          body: { user_id: sessionRes.circle_user_id, message },
        }),
      )
      if (signRes.signature) {
        return signRes.signature
      }
      if (!signRes.simulated && signRes.challenge_id) {
        const frame = signRunner ?? (await circleSignRunner())
        const sig = await frame.runSign({
          ...sessionRes,
          challenge_id: signRes.challenge_id,
        })
        if (sig) return sig
      }
      throw new Error('Could not obtain signature from Circle wallet')
    },
  }
}
