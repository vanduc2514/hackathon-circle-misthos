/**
 * The platform's fee, in cents, mirroring `MisthosEscrow.release` and
 * `domain/pricing.platform_fee`.
 *
 * One rate for everyone: 10 percent of the committed amount, taken on settlement and
 * refunded in full if the issue never settles. The floor division is the contract's,
 * so a figure shown here is the figure that moves.
 */

export const PLATFORM_FEE_BPS = 1000

/** The lowest price an issue may be published at (#142). */
export const MINIMUM_PRICE = 5

/** What the platform keeps of a commitment. */
export function platformFee(amount: number): number {
  return Math.floor((amount * PLATFORM_FEE_BPS) / 10_000)
}

/** What the contributor receives: the commitment less the fee. */
export function contributorShare(amount: number): number {
  return amount - platformFee(amount)
}

/** A price typed by a person, as a number, or NaN when it is not one. */
export function parseAmount(text: string): number {
  const named = Number(text.replace(/[,\s$]/g, ''))
  return Number.isFinite(named) ? named : Number.NaN
}
