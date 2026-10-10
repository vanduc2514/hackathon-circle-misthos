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
 */

import { createHash } from 'node:crypto'
import { initiateUserControlledWalletsClient } from '@circle-fin/user-controlled-wallets'
import { simulatedFrom } from './simulated.js'

export type Party = 'publisher' | 'contributor'

export const ARC_TESTNET = 'ARC-TESTNET'
export const ARC_MAINNET = 'ARC'

/** Circle's code for "a user with this id already exists". */
const USER_EXISTS = 155101

export interface WalletConfig {
  simulated: boolean
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
  simulated: boolean
}

export interface PartyWallet {
  circleUserId: string
  address: string | null
  blockchain: string
  simulated: boolean
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
}

/**
 * Read the wallets' configuration. `MISTHOS_SIMULATED` is read as the API and the x402
 * gate read it (simulated.ts). Only the literal `false` used to make these wallets
 * live, so with `0` a live API was handed made-up addresses and kept them as payout
 * wallets. Live, real wallets need Circle's credentials, and mainnet needs
 * EDGE_CONFIRM_MAINNET to name chain 5042.
 */
export function walletConfig(env: NodeJS.ProcessEnv = process.env): WalletConfig {
  const simulated = simulatedFrom(env.MISTHOS_SIMULATED)
  const blockchain = env.CIRCLE_WALLET_BLOCKCHAIN === ARC_MAINNET ? ARC_MAINNET : ARC_TESTNET
  const config: WalletConfig = {
    simulated,
    apiKey: env.CIRCLE_API_KEY ?? '',
    appId: env.CIRCLE_APP_ID ?? '',
    blockchain,
  }
  if (!simulated) {
    if (!config.apiKey || !config.appId) {
      throw new Error('CIRCLE_API_KEY and CIRCLE_APP_ID are required for real wallets')
    }
    if (blockchain === ARC_MAINNET && env.EDGE_CONFIRM_MAINNET !== '5042') {
      throw new Error('mainnet wallets hold real USDC: set EDGE_CONFIRM_MAINNET=5042 to confirm')
    }
  }
  return config
}

const PARTY_ID = /^[A-Za-z0-9_-]{1,64}$/

/** Circle requires user ids of at least five characters; ours are namespaced. */
export function circleUserId(party: Party, id: string): string {
  if (!PARTY_ID.test(id)) throw new Error(`invalid ${party} id`)
  return `misthos-${party}-${id}`
}

function simulatedAddress(userId: string): string {
  return `0x${createHash('sha256').update(userId).digest('hex').slice(0, 40)}`
}

function isUserExists(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { code?: unknown }).code === USER_EXISTS
}

export interface WalletService {
  config: WalletConfig
  session(party: Party, id: string): Promise<WalletSession>
  wallet(party: Party, id: string): Promise<PartyWallet>
}

export function createWalletService(
  config: WalletConfig = walletConfig(),
  client?: CircleUsers,
): WalletService {
  if (config.simulated) {
    return {
      config,
      session: async (party, id) => {
        const userId = circleUserId(party, id)
        return {
          circleUserId: userId,
          appId: 'simulated',
          userToken: 'simulated',
          encryptionKey: 'simulated',
          challengeId: null,
          address: simulatedAddress(userId),
          blockchain: config.blockchain,
          simulated: true,
        }
      },
      wallet: async (party, id) => {
        const userId = circleUserId(party, id)
        return {
          circleUserId: userId,
          address: simulatedAddress(userId),
          blockchain: config.blockchain,
          simulated: true,
        }
      },
    }
  }

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

  return {
    config,
    async session(party, id) {
      const userId = circleUserId(party, id)
      try {
        await circle.createUser({ userId })
      } catch (err) {
        if (!isUserExists(err)) throw err
      }
      const { userToken, encryptionKey } = await token(userId)
      const address = await arcAddress(userToken)

      let challengeId: string | null = null
      if (address === null) {
        // SCA so Circle's Gas Station can sponsor the first transaction; on Arc
        // gas is USDC, and a new contributor should not need USDC to get paid.
        const res = await circle.createUserPinWithWallets({
          userToken,
          blockchains: [config.blockchain],
          accountType: 'SCA',
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
        simulated: false,
      }
    },
    async wallet(party, id) {
      const userId = circleUserId(party, id)
      const { userToken } = await token(userId)
      return {
        circleUserId: userId,
        address: await arcAddress(userToken),
        blockchain: config.blockchain,
        simulated: false,
      }
    },
  }
}
