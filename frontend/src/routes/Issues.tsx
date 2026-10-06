import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, money, relativeTime, type IssueSummaryOut } from '../lib/client'
import { Panel, StateBadge } from '../components/ui'

const FILTERS = [
  { key: '', label: 'All' },
  { key: 'AWAITING_APPROVAL', label: 'Needs price approval' },
  { key: 'FUNDED', label: 'Funded, unclaimed' },
  { key: 'CLAIMED', label: 'Claimed' },
  { key: 'IN_REVIEW', label: 'In review' },
  { key: 'REWORK', label: 'Rework' },
  { key: 'PAID', label: 'Paid' },
  { key: 'PAID', label: 'Settled' },
  { key: 'REFUNDED', label: 'Refunded' },
]

export default function Issues() {
  const [state, setState] = useState('')
  const [complianceOnly, setComplianceOnly] = useState(false)

  const { data, isLoading, error } = useQuery({
    queryKey: ['issues', state, complianceOnly],
    queryFn: async () => {
      const res = await api.GET('/api/v1/issues', {
        params: {
          query: {
            ...(state ? { state } : {}),
            ...(complianceOnly ? { compliance_only: true } : {}),
          },
        },
      })
      return res.data as IssueSummaryOut[] | undefined
    },
  })

  const rows = data ?? []
  const total = rows.reduce((sum, r) => sum + Number(r.price_usdc ?? 0), 0)

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Issues</h1>
          <p className="page-sub">
            Every issue carries one fixed price, set before publication. The first
            contributor to claim it holds it exclusively. There is nothing to bid against
            because the price is already public.
          </p>
        </div>
      </div>

      <div className="filter-bar">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            className={`chip ${state === f.key ? 'accent' : ''}`}
            onClick={() => setState(f.key)}
            style={{ cursor: 'pointer', background: state === f.key ? undefined : 'transparent' }}
          >
            {f.label}
          </button>
        ))}
        <button
          className={`chip ${complianceOnly ? 'accent' : ''}`}
          onClick={() => setComplianceOnly((v) => !v)}
          style={{ cursor: 'pointer', background: complianceOnly ? undefined : 'transparent' }}
        >
          Compliance-driven only
        </button>
      </div>

      {error && <div className="error-box">Could not reach the API. Is the backend running on port 8000?</div>}

      <Panel flush>
        <table className="table">
          <thead>
            <tr>
              <th>Issue</th>
              <th>Publisher</th>
              <th>State</th>
              <th>Confidence</th>
              <th>Deadline</th>
              <th style={{ textAlign: 'right' }}>Price</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>
                  <Link to={`/issues/${r.id}`} className="row-link">
                    <div className="cell-title">
                      {r.title}
                      {r.compliance_driven && (
                        <span className="chip accent" style={{ marginLeft: 8 }}>
                          CRA
                        </span>
                      )}
                    </div>
                    <div className="cell-repo">
                      {r.repo}#{r.number}
                    </div>
                  </Link>
                </td>
                <td className="dim">{r.publisher_name}</td>
                <td>
                  <StateBadge state={r.state} />
                </td>
                <td className="dim">{r.confidence ?? '—'}</td>
                <td className="dim">{r.deadline ? relativeTime(r.deadline) : '—'}</td>
                <td className="mono-num" style={{ textAlign: 'right' }}>
                  ${money(r.price_usdc)}
                </td>
              </tr>
            ))}
          </tbody>
          {rows.length > 0 && (
            <tfoot>
              <tr>
                <td colSpan={5} className="dim">
                  {rows.length} issue{rows.length === 1 ? '' : 's'}
                </td>
                <td className="mono-num" style={{ textAlign: 'right' }}>
                  ${total.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
              </tr>
            </tfoot>
          )}
        </table>
        {isLoading && <div className="empty">Loading…</div>}
        {!isLoading && rows.length === 0 && <div className="empty">No issues match that filter.</div>}
      </Panel>
    </>
  )
}
