/**
 * The escrow term a publisher names (#172), mirroring `domain/issue.escrow_term`: whole
 * days from 3 to 90, 14 when not named. The server is the one that refuses; this lets
 * the form say why before it is sent.
 */

export const MIN_ESCROW_TERM_DAYS = 3
export const MAX_ESCROW_TERM_DAYS = 90
export const DEFAULT_ESCROW_TERM_DAYS = 14

/** Why a typed term would be refused, or null when it is a term the escrow will hold. */
export function termProblem(text: string): string | null {
  const value = Number(text.trim())
  if (text.trim() === '' || Number.isNaN(value)) return 'Name the term as a number of days.'
  if (!Number.isInteger(value)) return 'Name the term in whole days.'
  if (value < MIN_ESCROW_TERM_DAYS || value > MAX_ESCROW_TERM_DAYS) {
    return `Between ${MIN_ESCROW_TERM_DAYS} and ${MAX_ESCROW_TERM_DAYS} days.`
  }
  return null
}

/** The deadline a term implies for money committed at `from`. */
export function deadlineFor(days: number, from: Date): Date {
  return new Date(from.getTime() + days * 24 * 60 * 60 * 1000)
}

const DAY = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'long',
  year: 'numeric',
  timeZone: 'UTC',
})

/**
 * How long the money is held, as a contributor reads it before claiming: the term, and
 * the date it ends once the money is committed.
 */
export function termNote(days: number, deadline: string | null | undefined): string {
  const term = `${days} day${days === 1 ? '' : 's'}`
  return deadline ? `${term}, until ${DAY.format(new Date(deadline))}` : `${term} once funded`
}
