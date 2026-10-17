/**
 * A Circle wallet as a way in (#181): the gate's second door.
 *
 * The wallet is user-controlled: its owner sets a PIN in Circle's hosted frame and holds
 * their share of a 2-of-2 key. Signing in with it signs the very message MetaMask
 * signs, through a `SIGN_MESSAGE` challenge, and `POST /auth/verify` recovers the
 * address from the signature the same way. Neither the platform nor this page ever sees
 * the PIN or a key; the API only hands Circle's frame its challenges.
 *
 */

import { toChecksumAddress } from './siwe'
import type { Signer } from './wallet'

/** What the frame needs to run a challenge, as the API's Circle routes answer it. */
export interface CircleFrame {
  app_id: string
  user_token: string
  encryption_key: string
  challenge_id: string | null
}

export interface CircleSession extends CircleFrame {
  address: string | null
}

export interface CircleSignature extends CircleFrame {
  signature: string | null
}

/** The part of Circle's web SDK this uses, so tests can stand in for the frame. */
export interface ChallengeRunner {
  /** Runs the challenge; a `SIGN_MESSAGE` one answers with the signature. */
  run: (frame: CircleFrame & { challenge_id: string }) => Promise<{ signature?: string }>
}

/** The API's Circle routes, which the gate calls with this browser's sign-in id. */
export interface CircleApi {
  /** Starts or continues the sign-in: the PIN and wallet challenge while there is none. */
  session: () => Promise<CircleSession>
  /** Reads the address back, and asks Circle for nothing new. */
  wallet: () => Promise<{ address: string | null }>
  sign: (message: string) => Promise<CircleSignature>
}

async function circleRunner(): Promise<ChallengeRunner> {
  // Loaded on click: the SDK brings Circle's frame and its dependencies, which no
  // other page needs.
  const { W3SSdk } = await import('@circle-fin/w3s-pw-web-sdk')
  return {
    run: async (frame) => {
      const sdk = new W3SSdk({ appSettings: { appId: frame.app_id } })
      // Without the device id the frame has no session and `execute` silently does
      // nothing, so it is fetched before anything else.
      await sdk.getDeviceId()
      sdk.setAuthentication({ userToken: frame.user_token, encryptionKey: frame.encryption_key })
      return new Promise((resolve, reject) => {
        sdk.execute(frame.challenge_id, (error, result) => {
          if (error) {
            reject(new Error(error.message || 'Circle did not finish'))
            return
          }
          const status = (result as { status?: string } | undefined)?.status
          if (status && status !== 'COMPLETE' && status !== 'IN_PROGRESS') {
            reject(new Error(`Circle ended the request as ${status.toLowerCase()}`))
            return
          }
          const data = (result as { data?: { signature?: string } } | undefined)?.data
          resolve({ signature: data?.signature })
        })
      })
    },
  }
}

/**
 * The Circle wallet as a signer. A first visit runs the PIN challenge that creates the
 * wallet, and only then is there an address to sign in as; each signature is approved
 * with the PIN in Circle's frame.
 */
export async function circleSigner(
  api: CircleApi,
  runner?: ChallengeRunner,
  pauseMs = 1000,
): Promise<Signer> {
  let frame = runner ?? null
  const runInFrame = async (challenge: CircleFrame & { challenge_id: string }) => {
    frame ??= await circleRunner()
    return frame.run(challenge)
  }

  const session = await api.session()
  let address = session.address
  if (!address && session.challenge_id) {
    await runInFrame({ ...session, challenge_id: session.challenge_id })
    // Circle can still be creating the wallet when the frame closes, so the address is
    // read back a few times before the gate says to try again. Only read: starting the
    // session again would ask Circle for a second PIN and wallet.
    for (let tries = 0; tries < 5 && !address; tries++) {
      if (tries) await new Promise((resolve) => setTimeout(resolve, pauseMs))
      address = (await api.wallet()).address
    }
  }
  if (!address) throw new Error('Circle has not created your wallet yet; try again.')

  return {
    kind: 'circle',
    address: toChecksumAddress(address),
    sign: async (message) => {
      const answer = await api.sign(message)
      if (answer.signature) return answer.signature
      if (!answer.challenge_id) throw new Error('Circle sent nothing to sign.')
      const { signature } = await runInFrame({ ...answer, challenge_id: answer.challenge_id })
      if (!signature) throw new Error('Circle did not return a signature.')
      return signature
    },
  }
}
