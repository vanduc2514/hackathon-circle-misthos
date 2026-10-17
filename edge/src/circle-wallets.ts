/**
 * Circle user-controlled wallets, one per publisher and one per contributor.
 *
 * User-controlled, not developer-controlled, because the platform is never a
 * custodian. Circle splits the key 2-of-2 between itself and the user; the
 * platform holds an API key that can create a user and ask for a challenge, but
 * every wallet creation, transfer and signature is approved by the user with
 * their own PIN in Circle's hosted UI. We can read a wallet; we cannot move it.
 *
 * The flow, for a party the platform knows by id:
 *
 *   1. `session(id)` creates the Circle user if needed and returns a 60-minute
 *      user token, an encryption key, and, if the user has no Arc wallet yet, a
 *      challenge to create one.
 *   2. The browser executes the challenge with `@circle-fin/w3s-pw-web-sdk`; the
 *      user sets a PIN, and Circle creates the wallet.
 *   3. `wallet(id)` reads the address back, which the core records as where
 *      escrow releases pay out.
 *
 * Spending policies are a developer-controlled and agent-wallet feature, and
 * mainnet only. The guardrail that holds here is MisthosEscrow's per-issue
 * ceiling: an issue cannot take more than the price a human approved.
 *
 * A Circle wallet is also a way to sign in (#181), which comes before any party
 * exists. Those wallets are keyed by a random id the browser keeps, not by a party,
 * and they are EOAs: the API checks a sign-in by recovering the address from an
 * EIP-191 signature, and a smart account's EIP-1271 signature cannot be recovered.
 *
 *   1. `signInSession(id)` is `session` for that user, creating an EOA.
 *   2. `signInWallet(id)` reads its address back, creating nothing.
 *   3. `signInSignature(id, message)` asks Circle for a SIGN_MESSAGE challenge over
 *      the sign-in message; the browser runs it, the user enters their PIN, and the
 *      signature comes back from Circle's frame, never through here.
 */

import { initiateUserControlledWalletsClient } from '@circle-fin/user-controlled-wallets'

export type Party = 'publisher' | 'contributor'

export const ARC_TESTNET = 'ARC-TESTNET'
export const ARC_MAINNET = 'ARC'

/** Circle's code for "a user with this id already exists". */
const USER_EXISTS = 155101

export interface WalletConfig {
  apiKey: string
  appId: string
  blockchain: typeof ARC_TESTNET | typeof ARC_MAINNET
}

export interface WalletSession {
  circleUserId: string
  appId: string
  userToken: string
  encryptionKey: string
  /** Set when the user has no Arc wallet yet; the browser executes it. */
  challengeId: string | null
  address: string | null
  blockchain: string
}

export interface SignInChallenge {
  appId: string
  userToken: string
  encryptionKey: string
  /** What the browser runs in Circle's frame to sign. */
  challengeId: string | null
  /** Always null: the signature is made in the frame, never here. */
  signature: string | null
  address: string
  blockchain: string
}

export interface PartyWallet {
  circleUserId: string
  address: string | null
  blockchain: string
}

/** The subset of Circle's client this module calls, so tests can stand in for it. */
export interface CircleUsers {
  createUser(input: { userId: string }): Promise<unknown>
  createUserToken(input: {
    userId: string
  }): Promise<{ data?: { userToken: string; encryptionKey?: string } }>
  createUserPinWithWallets(input: {
    userToken: string
    blockchains: string[]
    accountType: 'SCA' | 'EOA'
  }): Promise<{ data?: { challengeId: string } }>
  listWallets(input: {
    userToken: string
    blockchain?: string
  }): Promise<{ data?: { wallets: { address: string; blockchain: string }[] } }>
  signMessage(input: {
    userToken: string
    walletAddress: string
    blockchain: string
    message: string
    encodedByHex: boolean
    memo: string
  }): Promise<{ data?: { challengeId: string } }>
}

/**
 * Read the wallets' configuration. Circle's credentials are required, and mainnet
 * needs `EDGE_CONFIRM_MAINNET` to name chain 5042.
 */
export function walletConfig(env: NodeJS.ProcessEnv = process.env): WalletConfig {
  const blockchain = env.CIRCLE_WALLET_BLOCKCHAIN === ARC_MAINNET ? ARC_MAINNET : ARC_TESTNET
  const config: WalletConfig = {
    apiKey: env.CIRCLE_API_KEY ?? '',
    appId: env.CIRCLE_APP_ID ?? '',
    blockchain,
  }
  if (!config.apiKey || !config.appId) {
    throw new Error('CIRCLE_API_KEY and CIRCLE_APP_ID are required for real wallets')
  }
  if (blockchain === ARC_MAINNET && env.EDGE_CONFIRM_MAINNET !== '5042') {
    throw new Error('mainnet wallets hold real USDC: set EDGE_CONFIRM_MAINNET=5042 to confirm')
  }
  return config
}

const PARTY_ID = /^[A-Za-z0-9_-]{1,64}$/

/** Circle requires user ids of at least five characters; ours are namespaced. */
export function circleUserId(party: Party, id: string): string {
  if (!PARTY_ID.test(id)) throw new Error(`invalid ${party} id`)
  return `misthos-${party}-${id}`
}

const SIGN_IN_ID = /^[A-Za-z0-9_-]{16,64}$/

/** The Circle user behind a sign-in with a Circle wallet, before any party exists. */
export function signInUserId(id: string): string {
  if (!SIGN_IN_ID.test(id)) throw new Error('invalid sign-in id')
  return `misthos-signin-${id}`
}

/** Only a sign-in message for the wallet that signs it is ever put in front of its owner. */
function namesWallet(message: string, address: string): boolean {
  const lines = message.replace(/\r\n/g, '\n').split('\n')
  return lines.length > 1 && lines[1].toLowerCase() === address.toLowerCase()
}

function isUserExists(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { code?: unknown }).code === USER_EXISTS
}

export interface WalletService {
  config: WalletConfig
  session(party: Party, id: string): Promise<WalletSession>
  wallet(party: Party, id: string): Promise<PartyWallet>
  signInSession(id: string): Promise<WalletSession>
  signInWallet(id: string): Promise<PartyWallet>
  signInSignature(id: string, message: string): Promise<SignInChallenge>
}

export function createWalletService(
  config: WalletConfig = walletConfig(),
  client?: CircleUsers,
): WalletService {
  const circle: CircleUsers =
    client ?? (initiateUserControlledWalletsClient({ apiKey: config.apiKey }) as unknown as CircleUsers)

  async function token(userId: string) {
    const res = await circle.createUserToken({ userId })
    if (!res.data) throw new Error('Circle returned no user token')
    return { userToken: res.data.userToken, encryptionKey: res.data.encryptionKey ?? '' }
  }

  async function arcAddress(userToken: string): Promise<string | null> {
    const res = await circle.listWallets({ userToken, blockchain: config.blockchain })
    const wallet = res.data?.wallets.find((w) => w.blockchain === config.blockchain)
    return wallet?.address ?? null
  }

  async function sessionFor(userId: string, accountType: 'SCA' | 'EOA'): Promise<WalletSession> {
    try {
      await circle.createUser({ userId })
    } catch (err) {
      if (!isUserExists(err)) throw err
    }
    const { userToken, encryptionKey } = await token(userId)
    const address = await arcAddress(userToken)

    let challengeId: string | null = null
    if (address === null) {
      const res = await circle.createUserPinWithWallets({
        userToken,
        blockchains: [config.blockchain],
        accountType,
      })
      challengeId = res.data?.challengeId ?? null
    }

    return {
      circleUserId: userId,
      appId: config.appId,
      userToken,
      encryptionKey,
      challengeId,
      address,
      blockchain: config.blockchain,
    }
  }

  return {
    config,
    // SCA so Circle's Gas Station can sponsor the first transaction; on Arc gas is
    // USDC, and a new contributor should not need USDC to get paid.
    session: (party, id) => sessionFor(circleUserId(party, id), 'SCA'),
    // EOA, because signing in is checked by recovering the address from the signature.
    signInSession: (id) => sessionFor(signInUserId(id), 'EOA'),
    async signInWallet(id) {
      const userId = signInUserId(id)
      const { userToken } = await token(userId)
      return {
        circleUserId: userId,
        address: await arcAddress(userToken),
        blockchain: config.blockchain,
        }
    },
    async signInSignature(id, message) {
      const { userToken, encryptionKey } = await token(signInUserId(id))
      const address = await arcAddress(userToken)
      if (address === null) throw new Error('invalid sign-in: the wallet is not created yet')
      if (!namesWallet(message, address)) throw new Error('invalid message: it names another wallet')
      const res = await circle.signMessage({
        userToken,
        walletAddress: address,
        blockchain: config.blockchain,
        message,
        encodedByHex: false,
        memo: 'Sign in to Misthos. This costs nothing and moves no money.',
      })
      if (!res.data?.challengeId) throw new Error('Circle returned no challenge to sign')
      return {
        appId: config.appId,
        userToken,
        encryptionKey,
        challengeId: res.data.challengeId,
        signature: null,
        address,
        blockchain: config.blockchain,
        }
    },
    async wallet(party, id) {
      const userId = circleUserId(party, id)
      const { userToken } = await token(userId)
      return {
        circleUserId: userId,
        address: await arcAddress(userToken),
        blockchain: config.blockchain,
        }
    },
  }
}
