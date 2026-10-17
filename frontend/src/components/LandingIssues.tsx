import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, money, relativeTime, type IssueSummaryOut } from '../lib/client'
import { GateLink } from './SignInGate'

/**
 * The landing page's "Current issues": the open backlog as the API holds it.
 *
 * The prototype drew this table from a mock list. It now reads `/api/v1/issues` once
 * and filters in the browser, so the count, the filters and the total always agree.
 * Each row opens the issue's own page in the app, once the sign-in gate is done; until
 * then it opens the gate (#181).
 */

/** The words the landing uses for each state, and the colour it gives it. */
const STATES: Record<string, { label: string; tone?: string }> = {
  DRAFT: { label: 'Draft' },
  PRICED: { label: 'Priced' },
  AWAITING_APPROVAL: { label: 'Needs price approval', tone: 'approval' },
  FUNDED: { label: 'Funded, unclaimed', tone: 'funded' },
  CLAIMED: { label: 'Claimed', tone: 'claimed' },
  IN_REVIEW: { label: 'In review', tone: 'review' },
  REWORK: { label: 'Rework', tone: 'rework' },
  ACCEPTED: { label: 'Awaiting merge', tone: 'merge' },
  PAID: { label: 'Settled', tone: 'settled' },
  REJECTED: { label: 'Rejected', tone: 'refunded' },
  REFUNDED: { label: 'Refunded', tone: 'refunded' },
}

const COMPLIANCE = 'COMPLIANCE'

const FILTERS: { key: string; label: string }[] = [
  { key: '', label: 'All' },
  ...['AWAITING_APPROVAL', 'FUNDED', 'CLAIMED', 'IN_REVIEW', 'REWORK', 'ACCEPTED', 'PAID', 'REFUNDED'].map(
    (key) => ({ key, label: STATES[key].label }),
  ),
  { key: COMPLIANCE, label: 'Compliance-driven only' },
]

/** Enough rows to show what is on offer; the Issues page has the rest. */
const SHOWN = 10

function matches(issue: IssueSummaryOut, filter: string): boolean {
  if (!filter) return true
  if (filter === COMPLIANCE) return issue.compliance_driven
  return issue.state === filter
}

export default function LandingIssues() {
  const [filter, setFilter] = useState('')
  const { data, isLoading, error } = useQuery({
    queryKey: ['issues'],
    queryFn: async () => (await api.GET('/api/v1/issues')).data as IssueSummaryOut[] | undefined,
  })

  const all = data ?? []
  const rows = useMemo(() => all.filter((issue) => matches(issue, filter)), [all, filter])
  const shown = rows.slice(0, SHOWN)
  const total = rows.reduce((sum, issue) => sum + Number(issue.price_usdc ?? 0), 0)

  return (
    <section className="current-issues" id="current-issues" aria-labelledby="current-issues-heading">
      <div className="current-issues-inner">
        <div className="current-issues-heading">
          <div>
            <span className="issues-kicker">THE OPEN BACKLOG</span>
            <h2 id="current-issues-heading">Current issues</h2>
            <p>Every issue carries one fixed price, set before anyone starts work.</p>
          </div>
          <span className="issues-count-badge">
            {all.length} {all.length === 1 ? 'ISSUE' : 'ISSUES'}
          </span>
        </div>

        <div className="issue-filters" role="group" aria-label="Filter issues by status">
          {FILTERS.map((f) => (
            <button
              className={`issue-filter${filter === f.key ? ' is-active' : ''}`}
              type="button"
              key={f.key || 'all'}
              aria-pressed={filter === f.key}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>

        <div className="issue-table-scroll">
          <table className="issue-table">
            <thead>
              <tr>
                <th scope="col">Issue</th>
                <th scope="col">Publisher</th>
                <th scope="col">State</th>
                <th scope="col">Confidence</th>
                <th scope="col">Deadline</th>
                <th scope="col" className="issue-price-column">
                  Price
                </th>
              </tr>
            </thead>
            <tbody aria-live="polite">
              {shown.map((issue) => {
                const state = STATES[issue.state] ?? { label: issue.state.replace(/_/g, ' ') }
                return (
                  <tr key={issue.id}>
                    <td className="issue-title-cell">
                      <GateLink to={`/issues/${issue.id}`}>
                        <strong>{issue.title}</strong>
                        <span>
                          {issue.repo}#{issue.number}
                        </span>
                      </GateLink>
                    </td>
                    <td>{issue.publisher_name}</td>
                    <td>
                      <span className={`issue-state${state.tone ? ` issue-state-${state.tone}` : ''}`}>
                        {state.label}
                      </span>
                    </td>
                    <td className={`issue-confidence${issue.confidence ? ` confidence-${issue.confidence}` : ''}`}>
                      {issue.confidence ?? '—'}
                    </td>
                    <td>{issue.deadline ? relativeTime(issue.deadline) : '—'}</td>
                    <td className="issue-price-column">
                      {issue.price_usdc == null ? '—' : `$${money(issue.price_usdc)}`}
                    </td>
                  </tr>
                )
              })}
              {isLoading && (
                <tr>
                  <td className="issue-empty" colSpan={6}>
                    Loading…
                  </td>
                </tr>
              )}
              {error && (
                <tr>
                  <td className="issue-empty" colSpan={6}>
                    The issues could not be loaded just now.
                  </td>
                </tr>
              )}
              {!isLoading && !error && rows.length === 0 && (
                <tr>
                  <td className="issue-empty" colSpan={6}>
                    No issues match this filter yet.
                  </td>
                </tr>
              )}
            </tbody>
            {rows.length > 0 && (
              <tfoot>
                <tr>
                  <td colSpan={4}>
                    {rows.length > SHOWN
                      ? `${SHOWN} of ${rows.length} issues`
                      : `${rows.length} ${rows.length === 1 ? 'issue' : 'issues'}`}
                  </td>
                  <td colSpan={2} className="issue-price-column">
                    ${money(total)}
                  </td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
        <p className="issues-note">
          <GateLink to="/issues">See every issue, with its price and its record →</GateLink>
        </p>
      </div>
    </section>
  )
}
