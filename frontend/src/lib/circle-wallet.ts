/**
 * Set up a party's own Circle wallet, where its payouts land (#29).
 *
 * The wallet is user-controlled: the person sets a PIN in Circle's hosted frame and
 * holds their share of a 2-of-2 key. The API starts a session and, when the party has
 * no Arc wallet yet, returns a challenge; this runs that challenge in the browser, and
 * the API then reads the new address back from Circle. Neither the platform nor this
 * page ever sees the PIN or a key.
 */

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
