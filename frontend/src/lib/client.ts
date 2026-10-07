import createClient from 'openapi-fetch'
import type { paths, components } from './api-schema'

/**
 * No baseUrl: requests go to /api/v1 on the same origin and Vite proxies them to
 * the API in development. One origin, so no CORS and no cookie surprises.
 */
export const api = createClient<paths>({})

export type IssueOut = components['schemas']['IssueOut']
export type IssueSummaryOut = components['schemas']['IssueSummaryOut']
export type MetricsOut = components['schemas']['MetricsOut']
export type TimelineEntry = components['schemas']['TimelineEntry']
export type Decision = components['schemas']['Decision']
// Anyone sees every publisher; its budget and policy only it, or the simulation.
export type Publisher = components['schemas']['PublisherListing']
export type LoopOut = components['schemas']['LoopOut']
export type SpendOut = components['schemas']['SpendOut']
export type FinanceOut = components['schemas']['FinanceOut']

export type Account = components['schemas']['Account']
export type MeOut = components['schemas']['MeOut']
export type SessionOut = components['schemas']['SessionOut']

/** A refusal from the API, carrying the reason it gave. */
export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** FastAPI's `detail`: a sentence, or a list of validation errors. */
export function detailOf(error: unknown): string | null {
  if (!error || typeof error !== 'object' || !('detail' in error)) return null
  const detail = (error as { detail: unknown }).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) =>
        item && typeof item === 'object' && 'msg' in item ? String(item.msg) : String(item),
      )
      .join('; ')
  }
  return null
}

/** The body of a successful call, or an ApiError with the API's own reason. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error === undefined && result.data !== undefined) return result.data
  const { status, statusText } = result.response
  throw new ApiError(status, detailOf(result.error) ?? `${status} ${statusText}`.trim())
}

export type Money = { usdc: string | number; base_units: number }

export const money = (value: unknown): string => {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object' && value !== null && 'usdc' in value) {
    const raw = (value as Money).usdc
    return Number(raw).toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  }
  if (typeof value === 'string' || typeof value === 'number') {
    // Tolerate a figure that arrives already grouped, such as "48,500.00".
    const amount = typeof value === 'string' ? Number(value.replace(/,/g, '')) : value
    if (Number.isNaN(amount)) return '—'
    return amount.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  }
  return '—'
}

export const shortTime = (iso: string): string =>
  new Date(iso).toLocaleString('en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })

export const relativeTime = (iso: string): string => {
  const diff = Date.now() - new Date(iso).getTime()
  const future = diff < 0
  const mins = Math.round(Math.abs(diff) / 60000)

  let magnitude: string
  if (mins < 60) magnitude = `${mins}m`
  else if (mins < 60 * 48) magnitude = `${Math.round(mins / 60)}h`
  else magnitude = `${Math.round(mins / (60 * 24))}d`

  // A deadline is usually in the future, so "ago" alone reads as a bug.
  return future ? `in ${magnitude}` : `${magnitude} ago`
}

export const shortHash = (value: string): string =>
  value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value
