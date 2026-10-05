/**
 * Circle CLI bridge.
 *
 * Agent wallets are documented around the Circle CLI, which is a Node package.
 * The Python core delegates wallet operations here rather than shelling out
 * itself, so there is one place that knows the CLI's argument shapes.
 *
 * Spending policies are the interesting case and come with a caveat that will
 * bite: they are mainnet only. Setting one requires a mainnet agent wallet and
 * triggers an email OTP. Testnet is not supported, so the guardrail cannot be
 * demonstrated on testnet through Circle's mechanism. That is one of the reasons
 * MisthosEscrow enforces a per-issue ceiling itself.
 */

export type WalletOp = 'transfer' | 'bridge' | 'swap' | 'limit'

export interface WalletStatus {
  configured: boolean
  address: string | null
  chain: string
  cliInstalled: boolean
  policiesSupported: boolean
  note: string
}

export async function walletStatus(): Promise<WalletStatus> {
  return {
    configured: false,
    address: null,
    chain: 'ARC',
    cliInstalled: false,
    policiesSupported: false,
    note:
      'Circle CLI not invoked. Run `npm install -g @circle-fin/cli` then ' +
      '`circle wallet status` to enable agent wallet operations. Spending ' +
      'policies require a mainnet wallet and an email OTP to set.',
  }
}

/**
 * The commands this service would run, kept here as documentation until the CLI
 * is wired in:
 *
 *   circle wallet status
 *   circle wallet limit set --address 0x... --chain ARC \
 *     --policy-type stablecoin --per-tx 100 --daily 500 --weekly 2000 --monthly 5000
 *   circle wallet limit --address 0x... --chain ARC
 *
 * Limits must satisfy: per-transaction <= daily <= weekly <= monthly.
 */
export const POLICY_CONSTRAINT = 'per-transaction <= daily <= weekly <= monthly'
